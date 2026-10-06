"""Owner-only workspace settings page (``/settings``).

Plain HTML forms post to one route per card. Values live in the per-workspace
store (``app.workspace_settings``); channels and the Telegram connection are
managed directly in their own tables. Secrets are never rendered back.
"""

from __future__ import annotations

import logging
import re
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import limits
from ..session_crypto import build_cipher
from ..studio.search_health import configured_search_state
from ..studio.setup import build_setup_state
from ..workspace_settings import SETTINGS, EncryptionKeyRequired, WorkspaceSettings, format_timestamp
from .dependencies import require_auth
from .links import normalize_channel_identifier

_LOG_STATUSES = {"queued", "running", "succeeded", "failed", "cancelled", "interrupted"}
_LOGS_PAGE_SIZE_DEFAULT = 25
_LOGS_PAGE_SIZE_MAX = 100

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
log = logging.getLogger("settings_routes")

ENCRYPTION_KEY_MESSAGE = "Set TELEGRAM_SESSION_ENCRYPTION_KEY to store secrets."

_FRIENDLY_ERRORS = {
    "poll_minutes": "Poll interval must be between 1 and 1440 minutes.",
    "track_days": "History window must be between 0 and 3650 days.",
    "backfill_limit": "Backfill limit must be between 0 and 100000 posts.",
    "model": "Model must be 1 to 200 characters without spaces.",
    "openrouter_api_key": "API key must be at most 512 characters.",
    "provider": "Choose a supported model provider.",
    "ollama_url": "Use a local Ollama host and port, without a custom path.",
    "ollama_model": "Choose an installed model without whitespace in its identifier.",
    "openai_model": "Choose an available ChatGPT model.",
    "blocked_domains": "Blocked domains must be comma separated hostnames.",
}

_CHANNEL_MESSAGE = "Channel identifier must be @name, t.me/name, or -100…"
_USERNAME_RE = re.compile(r"[A-Za-z0-9_]{5,32}")


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    if "application/json" in accept.lower():
        if request.query_params.get("format") == "html":
            return False
        return True
    # In-place saves from the new UI post JSON or set ?format=json.
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type.lower():
        return True
    return request.query_params.get("format") == "json"


def _json_error(code: str, message: str, status_code: int = 422) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": message, "retryable": False}},
        status_code=status_code,
    )


async def _canonical_settings_json(request: Request, section: str, notice: str) -> dict[str, Any]:
    """Stable canonical response for in-place Settings saves (no secret echo)."""

    context = await _page_context(request)
    ws_dict = dict(context.get("ws_dict") or {})
    # Secrets are already write-only in as_dict ({set, source}); never echo plaintext.
    return {
        "ok": True,
        "section": section,
        "notice": notice,
        "fields": ws_dict,
        "connection": context.get("connection_status"),
        "setup": context.get("setup_state"),
        "search": context.get("search_state"),
        "restart_required": bool(context.get("restart_required", False)),
    }


def _parse_log_filters(request: Request) -> tuple[str | None, int | None, str | None, int, int, JSONResponse | None]:
    params = request.query_params
    raw_query = params.get("q", "")
    query = raw_query.strip() or None
    if query is not None and len(query) > 200:
        return None, None, None, 1, _LOGS_PAGE_SIZE_DEFAULT, _json_error("invalid_query", "Invalid query: must be at most 200 characters.", 422)
    raw_channel = params.get("channel", "")
    channel_id: int | None = None
    if raw_channel.strip():
        try:
            channel_id = int(raw_channel.strip())
        except (TypeError, ValueError):
            return None, None, None, 1, _LOGS_PAGE_SIZE_DEFAULT, _json_error("invalid_channel", "Invalid channel: expected a positive integer.", 422)
        if channel_id <= 0:
            return None, None, None, 1, _LOGS_PAGE_SIZE_DEFAULT, _json_error("invalid_channel", "Invalid channel: expected a positive integer.", 422)
    raw_status = params.get("status", "")
    status: str | None = raw_status.strip().lower() or None
    if status is not None and status not in _LOG_STATUSES:
        return None, None, None, 1, _LOGS_PAGE_SIZE_DEFAULT, _json_error(
            "invalid_status",
            "Invalid status: expected one of queued, running, succeeded, failed, cancelled, interrupted.",
            422,
        )
    try:
        page = int(str(params.get("page", "1")).strip() or "1")
    except (TypeError, ValueError):
        return None, None, None, 1, _LOGS_PAGE_SIZE_DEFAULT, _json_error("invalid_page", "Invalid page: expected >= 1.", 422)
    if page < 1 or page > 10000:
        return None, None, None, 1, _LOGS_PAGE_SIZE_DEFAULT, _json_error("invalid_page", "Invalid page: expected 1..10000.", 422)
    try:
        page_size = int(str(params.get("page_size", str(_LOGS_PAGE_SIZE_DEFAULT))).strip() or str(_LOGS_PAGE_SIZE_DEFAULT))
    except (TypeError, ValueError):
        return None, None, None, 1, _LOGS_PAGE_SIZE_DEFAULT, _json_error("invalid_page", "Invalid page_size: expected 1..100.", 422)
    if not 1 <= page_size <= _LOGS_PAGE_SIZE_MAX:
        return None, None, None, 1, _LOGS_PAGE_SIZE_DEFAULT, _json_error("invalid_page", "Invalid page_size: expected 1..100.", 422)
    return query, channel_id, status, page, page_size, None


def _csrf_token(request: Request) -> str:
    try:
        return request.app.state.csrf_token(request)
    except Exception:  # noqa: BLE001 - template rendering must not fail on a missing token
        return ""


templates.env.globals["csrf_token"] = _csrf_token


async def _require_csrf(request: Request) -> None:
    expected = request.session.get("studio_csrf_token")
    supplied = request.headers.get("x-csrf-token", "")
    if not supplied:
        try:
            form = await request.form()
            supplied = str(form.get("csrf_token", "") or "")
        except Exception:  # noqa: BLE001 - a body that is not a form simply has no token
            supplied = ""
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


def _is_owner(request: Request) -> bool:
    context = getattr(request.app.state, "workspace_context", None)
    return getattr(context, "role", None) == "owner"


def _guard(request: Request) -> RedirectResponse | None:
    """Authenticated owner only. Anonymous browsers are sent to the login page."""
    try:
        require_auth(request)
    except HTTPException as exc:
        if exc.status_code in (303, 401):
            return RedirectResponse("/login", status_code=303)
        raise
    if not _is_owner(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return None


def _store(request: Request) -> WorkspaceSettings | None:
    return getattr(request.app.state, "workspace_settings", None)


def _available(request: Request) -> bool:
    store = _store(request)
    return bool(store is not None and getattr(store, "available", False))


def _flash(request: Request, message: str, section: str) -> None:
    request.session["settings_notice"] = {"message": message, "section": section}


def _redirect(section: str) -> RedirectResponse:
    return RedirectResponse(f"/settings#{section}", status_code=303)


def validate_channel_identifier(value: str) -> str | None:
    """Return an error message, or ``None`` when the identifier is acceptable."""
    v = value.strip()
    if not v:
        return "Channel identifier is required."
    if " " in v:
        return "Channel identifier must not contain spaces."
    if v.startswith("@"):
        return None if _USERNAME_RE.fullmatch(v[1:]) else _CHANNEL_MESSAGE
    if v.startswith(("https://t.me/", "http://t.me/", "t.me/")):
        name = v.split("t.me/", 1)[1].split("/")[0].split("?")[0]
        return None if _USERNAME_RE.fullmatch(name) else _CHANNEL_MESSAGE
    if v.startswith("-100"):
        return None if re.fullmatch(r"-100\d{5,}", v) else _CHANNEL_MESSAGE
    return _CHANNEL_MESSAGE


async def _page_context(
    request: Request,
    *,
    errors: dict[str, str] | None = None,
    values: dict[str, Any] | None = None,
    msg: str | None = None,
) -> dict[str, Any]:
    db = getattr(request.app.state, "db", None)
    store = _store(request)
    settings = getattr(request.app.state, "settings", None)

    channels: list[dict[str, Any]] = []
    channel_reader = getattr(db, "get_channels_for_settings", None) or getattr(db, "get_channels", None)
    if channel_reader is not None:
        try:
            channels = [dict(row) for row in await (channel_reader())]
        except Exception:  # noqa: BLE001 - render the page even when the channel query fails
            channels = []
    summaries: dict[int, dict[str, int]] = {}
    summary_reader = getattr(db, "channel_data_summaries", None)
    if summary_reader is not None:
        try:
            summaries = dict(await (summary_reader()))
        except Exception:  # noqa: BLE001 - counts are explanatory, not required for setup
            log.exception("Could not load channel archive counts for Settings")
    for channel in channels:
        channel["data_summary"] = summaries.get(
            int(channel["id"]),
            {"posts": 0, "comments": 0, "conversations": 0, "drafts": 0},
        )

    connection: dict[str, Any] = {"configured": False, "api_id": None, "has_session": False, "updated_at": None}
    if db is not None and hasattr(db, "telegram_connection_status"):
        try:
            connection = dict(await db.telegram_connection_status(limits.TELEGRAM_CONNECTION_LABEL))
        except Exception:  # noqa: BLE001
            pass
    connection["updated_at"] = format_timestamp(connection.get("updated_at"))

    ws_dict: dict[str, Any] = {}
    if store is not None:
        try:
            ws_dict = store.as_dict()
        except Exception:  # noqa: BLE001
            ws_dict = {}

    setup_state: dict[str, Any] = {}
    search_state: dict[str, Any] = {}
    if settings is not None:
        try:
            setup_state = build_setup_state(settings, db)
        except Exception:  # noqa: BLE001
            setup_state = {"ready": False, "blockers": [{"message": "Setup unavailable"}]}
        try:
            search_state = configured_search_state(settings).as_dict()
        except Exception:  # noqa: BLE001
            search_state = {}

    flash = msg if msg is not None else request.session.pop("flash_msg", "")
    # Optional provider fields also render during setup, before a store exists.
    from ..workspace_settings import SETTINGS
    for key, (kind, attr, _validator) in SETTINGS.items():
        if key not in ws_dict:
            default = getattr(settings, attr, "")
            ws_dict[key] = {"set": bool(default), "source": "default"} if kind == "secret" else {"value": default, "source": "default"}
    save_notice = request.session.pop("settings_notice", None)
    return {
        "request": request,
        "channels": channels,
        "connection_status": connection,
        "ws_dict": ws_dict,
        "available": _available(request),
        "errors": errors or {},
        "values": values or ({"provider": "openai"} if request.query_params.get("provider") == "openai" else {}),
        "msg": flash,
        "save_notice": save_notice if isinstance(save_notice, dict) else None,
        "can_manage_settings": _is_owner(request),
        "setup_state": setup_state,
        "search_state": search_state,
        "search_base_url": str(getattr(settings, "studio_search_base_url", "") or "").strip(),
        "restart_required": bool(getattr(store, "telegram_restart_required", False)),
    }


async def _render(
    request: Request,
    *,
    errors: dict[str, str] | None = None,
    values: dict[str, Any] | None = None,
    status_code: int = 200,
    msg: str | None = None,
) -> HTMLResponse:
    context = await _page_context(request, errors=errors, values=values, msg=msg)
    return templates.TemplateResponse(request, "settings.html", context, status_code=status_code)


router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_class=HTMLResponse)
async def settings_page(request: Request):
    if (redirect := _guard(request)) is not None:
        # JSON callers receive 401/303? Preserve redirect for browsers; JSON gets 401.
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": {"code": "unauthenticated", "message": "Sign in to continue.", "retryable": False}}, status_code=401)
        return redirect
    if _wants_json(request):
        context = await _page_context(request)
        return JSONResponse(
            {
                "ok": True,
                "fields": context.get("ws_dict"),
                "connection": context.get("connection_status"),
                "setup": context.get("setup_state"),
                "search": context.get("search_state"),
                "restart_required": bool(context.get("restart_required", False)),
                "available": bool(context.get("available", False)),
            }
        )
    return await _render(request)


@router.get("/logs", response_class=HTMLResponse)
async def settings_logs_page(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(_LOGS_PAGE_SIZE_DEFAULT, ge=1, le=_LOGS_PAGE_SIZE_MAX),
):
    """Show private full Studio tool outputs to the workspace owner.

    Server-side query/channel/status filtering with count/pagination over
    stored results.  JSON (`Accept: application/json` or `?format=json`)
    returns canonical fields including requested/actual model, run state and
    allowlisted diagnostics; ``available:false`` is storage unavailability,
    distinct from empty data.
    """

    if (redirect := _guard(request)) is not None:
        if _wants_json(request):
            return JSONResponse({"ok": False, "available": False, "error": {"code": "unauthenticated", "message": "Sign in to continue.", "retryable": False}}, status_code=401)
        return redirect
    query, channel_id, status, parsed_page, parsed_size, filter_error = _parse_log_filters(request)
    # FastAPI already validated page/page_size bounds; keep manual parse as source of truth when present.
    if filter_error is not None:
        if _wants_json(request):
            return filter_error
        raise HTTPException(status_code=filter_error.status_code, detail=bytes(filter_error.body).decode("utf-8") if hasattr(filter_error, "body") else "Invalid logs filter")
    # Prefer FastAPI-validated values when query string omits the new filters.
    page = parsed_page
    page_size = parsed_size
    repository = getattr(request.app.state, "studio_repository", None)
    getter = getattr(repository, "list_tool_result_logs", None)
    counter = getattr(repository, "count_tool_result_logs", None)
    try:
        if getter is None:
            raise RuntimeError("Studio log storage is unavailable")
        try:
            logs = list(await getter(limit=page_size + 1, offset=(page - 1) * page_size, query=query, channel_id=channel_id, status=status, include_runs=True))
        except TypeError:
            # Backward-compatible repository without filters: paginate unfiltered, then filter in memory.
            logs = list(await getter(limit=page_size + 1, offset=(page - 1) * page_size))
        try:
            total = int(await counter(query=query, channel_id=channel_id, status=status, include_runs=True)) if counter is not None else (len(logs) if len(logs) <= page_size else (page - 1) * page_size + len(logs))
        except TypeError:
            total = len(logs)
        available = True
    except ValueError as exc:
        if _wants_json(request):
            return _json_error("invalid_filter", str(exc), 422)
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:  # noqa: BLE001 - settings must remain available if Studio storage is unavailable
        if _wants_json(request):
            return JSONResponse(
                {"ok": False, "available": False, "error": {"code": "logs_unavailable", "message": "Agent log storage is unavailable.", "retryable": True}},
                status_code=503,
            )
        logs = []
        total = 0
        available = False
    has_next = page * page_size < total
    logs = logs[:page_size]
    for row in logs:
        row["created_at_display"] = format_timestamp(row.get("created_at"))
        safe_payload = row.get("safe_payload")
        if not row.get("tool_name"):
            row["tool_name"] = safe_payload.get("tool_name", "Tool") if isinstance(safe_payload, dict) else "Tool"
    if _wants_json(request):
        total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
        payload_logs = []
        for row in logs:
            diagnostics = row.get("diagnostics")
            if isinstance(diagnostics, dict):
                safe_diag = {
                    "phase": diagnostics.get("phase"),
                    "exception_class": diagnostics.get("exception_class"),
                    "status_code": diagnostics.get("status_code"),
                    "provider_code": diagnostics.get("provider_code"),
                }
            else:
                safe_diag = None
            payload_logs.append(
                {
                    "id": int(row.get("id", 0) or 0),
                    "run_id": str(row.get("run_id") or ""),
                    "sequence": int(row.get("sequence", 0) or 0),
                    "tool_name": str(row.get("tool_name") or "Tool"),
                    "conversation_id": str(row.get("conversation_id") or ""),
                    "conversation_title": str(row.get("conversation_title") or ""),
                    "channel_id": int(row["channel_id"]) if row.get("channel_id") is not None else None,
                    "requested_model": row.get("requested_model"),
                    "actual_model": row.get("actual_model"),
                    "run_status": row.get("run_status") or row.get("status") or "unknown",
                    "diagnostics": safe_diag,
                    "created_at": str(row.get("created_at") or ""),
                    "created_at_display": row.get("created_at_display"),
                }
            )
        return JSONResponse(
            {
                "ok": True,
                "available": True,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "has_next": has_next,
                "logs": payload_logs,
            }
        )
    return templates.TemplateResponse(
        request,
        "settings_logs.html",
        {
            "request": request,
            "logs": logs,
            "logs_available": available,
            "page": page,
            "has_next": has_next,
            "can_manage_settings": True,
        },
    )


async def _begin_write(request: Request) -> RedirectResponse | HTMLResponse | JSONResponse | None:
    """Shared prelude for every POST: auth, owner, CSRF, store availability."""
    if (redirect := _guard(request)) is not None:
        if _wants_json(request):
            return JSONResponse(
                {"ok": False, "error": {"code": "unauthenticated", "message": "Sign in to continue.", "retryable": False}},
                status_code=401,
            )
        return redirect
    try:
        await _require_csrf(request)
    except HTTPException:
        if _wants_json(request):
            return JSONResponse(
                {"ok": False, "error": {"code": "csrf_failed", "message": "CSRF validation failed.", "retryable": False}},
                status_code=403,
            )
        raise
    if not _available(request):
        if _wants_json(request):
            return JSONResponse(
                {"ok": False, "available": False, "error": {"code": "store_unavailable", "message": "Settings storage is unavailable.", "retryable": True}},
                status_code=503,
            )
        return await _render(request)
    return None


@router.post("/channels/add")
async def add_channel(request: Request, identifier: str = Form("")):
    if (early := await _begin_write(request)) is not None:
        return early
    # Support JSON callers that post {"identifier": "..."} without a form.
    if not identifier.strip() and "application/json" in request.headers.get("content-type", "").lower():
        try:
            body = await request.json()
            identifier = str(body.get("identifier", "") or "")
        except Exception:  # noqa: BLE001 - fall through to validation
            identifier = ""
    error = validate_channel_identifier(identifier)
    new_id: int | None = None
    if error is None:
        try:
            new_id = await request.app.state.db.add_channel(normalize_channel_identifier(identifier))
        except ValueError as exc:
            error = str(exc)
    if error is not None:
        if _wants_json(request):
            return JSONResponse({"ok": False, "errors": {"identifier": error}, "values": {"identifier": identifier}}, status_code=422)
        return await _render(request, errors={"identifier": error}, values={"identifier": identifier}, status_code=422)
    notice = f"Channel {identifier.strip()} added."
    _flash(request, notice, "telegram")
    if _wants_json(request):
        payload = await _canonical_settings_json(request, "telegram", notice)
        payload["channel"] = {"id": new_id, "identifier": identifier.strip()}
        return payload
    return _redirect("telegram")


@router.post("/channels/{channel_id}/deactivate")
async def deactivate_channel(request: Request, channel_id: int):
    if (early := await _begin_write(request)) is not None:
        return early
    db = request.app.state.db
    label = f"#{channel_id}"
    try:
        for row in await (db.get_channels()):
            if int(row["id"]) == channel_id:
                label = row["identifier"]
                break
    except Exception:  # noqa: BLE001 - the label is cosmetic
        pass
    await db.deactivate_channel(channel_id)
    notice = f"Channel {label} deactivated."
    _flash(request, notice, "telegram")
    if _wants_json(request):
        payload = await _canonical_settings_json(request, "telegram", notice)
        payload["channel_id"] = channel_id
        return payload
    return _redirect("telegram")


@router.post("/channels/{channel_id}/delete")
async def delete_channel(request: Request, channel_id: int, confirmation: str = Form("")):
    """Permanently delete one workspace channel and its dependent data."""

    if (early := await _begin_write(request)) is not None:
        return early
    if not confirmation.strip() and "application/json" in request.headers.get("content-type", "").lower():
        try:
            body = await request.json()
            confirmation = str(body.get("confirmation", "") or "")
        except Exception:  # noqa: BLE001 - fall through to validation
            confirmation = ""
    delete = getattr(request.app.state.db, "delete_channel", None)
    if delete is None:
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": {"code": "unavailable", "message": "Channel deletion is unavailable.", "retryable": False}}, status_code=501)
        raise HTTPException(status_code=501, detail="Channel deletion is unavailable")
    try:
        deleted = await (delete(channel_id, confirmation=confirmation))
    except ValueError as exc:
        if _wants_json(request):
            return JSONResponse({"ok": False, "errors": {"channel_delete": str(exc)}, "values": {"delete_channel_id": channel_id}}, status_code=422)
        return await _render(
            request,
            errors={"channel_delete": str(exc)},
            values={"delete_channel_id": channel_id},
            status_code=422,
        )
    if deleted is None:
        if _wants_json(request):
            return JSONResponse({"ok": False, "errors": {"channel_delete": "Channel not found."}, "values": {"delete_channel_id": channel_id}}, status_code=404)
        return await _render(
            request,
            errors={"channel_delete": "Channel not found."},
            values={"delete_channel_id": channel_id},
            status_code=404,
        )
    label = str(deleted.get("identifier") or f"#{channel_id}")
    log.info(
        "Deleted channel %s from workspace: posts=%s comments=%s conversations=%s drafts=%s",
        label,
        deleted.get("posts", 0),
        deleted.get("comments", 0),
        deleted.get("conversations", 0),
        deleted.get("drafts", 0),
    )
    notice = f"Channel {label} and all of its data were permanently deleted."
    _flash(request, notice, "telegram")
    if _wants_json(request):
        payload = await _canonical_settings_json(request, "telegram", notice)
        payload["deleted"] = deleted
        return payload
    return _redirect("telegram")


@router.post("/telegram-connection")
async def save_telegram_connection(
    request: Request,
    api_id: str = Form(""),
    api_hash: str = Form(""),
    session_string: str = Form(""),
):
    if (early := await _begin_write(request)) is not None:
        return early
    db = request.app.state.db
    settings = request.app.state.settings
    errors: dict[str, str] = {}
    values: dict[str, Any] = {}

    api_id_value: int | None = None
    if api_id.strip():
        try:
            api_id_value = int(api_id.strip())
            if api_id_value <= 0:
                raise ValueError
            values["api_id"] = api_id_value
        except ValueError:
            errors["api_id"] = "API ID must be a positive integer."
    if not errors and not (api_id.strip() or api_hash.strip() or session_string.strip()):
        errors["api_id"] = "Enter an API ID, an API hash, or a session string to save."
    if errors:
        if _wants_json(request):
            return JSONResponse({"ok": False, "errors": errors, "values": values}, status_code=422)
        return await _render(request, errors=errors, values=values, status_code=422)

    cipher = build_cipher(str(getattr(settings, "telegram_session_encryption_key", "") or ""))
    if cipher is None:
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": {"code": "encryption_key_required", "message": ENCRYPTION_KEY_MESSAGE, "retryable": False}}, status_code=409)
        return await _render(request, values=values, status_code=409, msg=ENCRYPTION_KEY_MESSAGE)

    existing: dict[str, Any] = {}
    reader = getattr(db, "telegram_connection_secrets", None)
    if reader is not None:
        try:
            existing = dict(await reader(limits.TELEGRAM_CONNECTION_LABEL, cipher=cipher) or {})
        except Exception:  # noqa: BLE001 - treated as no stored connection
            existing = {}

    final_api_id = api_id_value if api_id_value is not None else existing.get("api_id")
    final_api_hash = api_hash.strip() or existing.get("api_hash")
    final_session = session_string.strip() or existing.get("session_string")
    if not final_api_id:
        errors["api_id"] = "Required for a new connection."
    if not final_api_hash:
        errors["api_hash"] = "Required for a new connection."
    if not final_session:
        errors["session_string"] = "Required for a new connection."
    if errors:
        if _wants_json(request):
            return JSONResponse({"ok": False, "errors": errors, "values": values}, status_code=422)
        return await _render(request, errors=errors, values=values, status_code=422)

    assert final_api_id is not None
    await db.persist_telegram_session(
        label=limits.TELEGRAM_CONNECTION_LABEL,
        api_id=int(final_api_id),
        api_hash=str(final_api_hash),
        session_string=str(final_session),
        cipher=cipher,
    )
    store = _store(request)
    if store is not None:
        store.telegram_restart_required = True
    notice = "Connection saved. Restart the collector to use the updated credentials."
    _flash(request, notice, "telegram")
    if _wants_json(request):
        # Write-only secrets: blank keeps current values; never echo plaintext.
        payload = await _canonical_settings_json(request, "telegram", notice)
        return payload
    return _redirect("telegram")


def _validate_field(store: WorkspaceSettings, key: str, field: str, value: Any, errors: dict[str, str]) -> None:
    try:
        validator = getattr(store, "validate", None)
        if validator is not None:
            validator(key, value)
        else:
            SETTINGS[key][2](value)
    except ValueError:
        errors[field] = _FRIENDLY_ERRORS.get(field, "Invalid value.")


async def _apply_fields(
    store: WorkspaceSettings,
    changes: dict[str, Any],
    *,
    reset_keys: tuple[str, ...] = (),
) -> None:
    apply_many = getattr(store, "set_many", None)
    if apply_many is not None:
        await apply_many(changes, reset_keys=reset_keys)
        return
    for key in reset_keys:
        await store.reset(key)
    for key, value in changes.items():
        await store.set(key, value)


@router.post("/collection")
async def save_collection(
    request: Request,
    poll_minutes: str = Form(""),
    track_days: str = Form(""),
    backfill_limit: str = Form(""),
):
    if (early := await _begin_write(request)) is not None:
        return early
    store = _store(request)
    assert store is not None  # _begin_write guarantees an available store
    errors: dict[str, str] = {}
    values = {"poll_minutes": poll_minutes, "track_days": track_days, "backfill_limit": backfill_limit}
    changes: dict[str, Any] = {}
    for field, key, raw, cast in (
        ("poll_minutes", "collection.poll_minutes", poll_minutes, float),
        ("track_days", "collection.track_days", track_days, int),
        ("backfill_limit", "collection.backfill_limit", backfill_limit, int),
    ):
        try:
            typed = cast(raw.strip())
        except ValueError:
            errors[field] = _FRIENDLY_ERRORS[field]
            continue
        _validate_field(store, key, field, typed, errors)
        changes[key] = typed
    if errors:
        if _wants_json(request):
            return JSONResponse({"ok": False, "errors": errors, "values": values}, status_code=422)
        return await _render(request, errors=errors, values=values, status_code=422)
    # No partial apply: validation precedes the single atomic transaction.
    await _apply_fields(store, changes)
    # In the combined process, update the live scheduler immediately after a
    # successful persistence.  The worker process has no callback here and
    # will detect the persisted value during its next polling cycle.
    if "collection.poll_minutes" in changes:
        callback = getattr(request.app.state, "on_poll_interval_change", None)
        if callable(callback):
            try:
                await (callback(float(changes["collection.poll_minutes"])))
            except Exception as exc:  # noqa: BLE001 - settings save must still succeed
                log.warning("poll interval callback failed (%s)", type(exc).__name__)
    notice = "Collection settings saved."
    _flash(request, notice, "collection")
    if _wants_json(request):
        return await _canonical_settings_json(request, "collection", notice)
    return _redirect("collection")


@router.post("/studio")
async def save_studio(
    request: Request,
    openrouter_api_key: str = Form(""),
    clear_openrouter_api_key: str = Form(""),
    model: str = Form(""),
    provider: str = Form(""),
    ollama_url: str = Form(""),
    ollama_model: str = Form(""),
    openai_model: str = Form(""),
):
    if (early := await _begin_write(request)) is not None:
        return early
    store = _store(request)
    assert store is not None  # _begin_write guarantees an available store
    errors: dict[str, str] = {}
    values: dict[str, Any] = {"model": model, "provider": provider, "ollama_url": ollama_url, "ollama_model": ollama_model, "openai_model": openai_model}
    effective = store.effective if hasattr(store, "effective") else request.app.state.settings
    changes: dict[str, Any] = {}
    reset_keys: tuple[str, ...] = ()
    try:
        for field, value, key in (
            ("provider", provider, "studio.provider"), ("ollama_url", ollama_url, "studio.ollama_url"),
            ("ollama_model", ollama_model, "studio.ollama_model"), ("openai_model", openai_model, "studio.openai_model"),
        ):
            if value.strip():
                _validate_field(store, key, field, value, errors)
                changes[key] = value.strip()
        if clear_openrouter_api_key == "1":
            reset_keys = ("studio.openrouter_api_key",)
        elif openrouter_api_key.strip():
            key_value = openrouter_api_key.strip()
            _validate_field(store, "studio.openrouter_api_key", "openrouter_api_key", key_value, errors)
            changes["studio.openrouter_api_key"] = key_value
        if model.strip():
            model_value = model.strip()
            _validate_field(store, "studio.model", "model", model_value, errors)
            changes["studio.model"] = model_value
        elif (provider or effective.studio_provider) == "openrouter":
            errors["model"] = "Model is required."
        if (provider or effective.studio_provider) == "ollama" and not (ollama_model or effective.ollama_model).strip():
            errors["ollama_model"] = "Choose an installed Ollama model."
        if (provider or effective.studio_provider) == "openai" and not (openai_model or effective.openai_model).strip():
            errors["openai_model"] = "Connect ChatGPT, load models and choose one."
    except EncryptionKeyRequired:
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": {"code": "encryption_key_required", "message": ENCRYPTION_KEY_MESSAGE, "retryable": False}}, status_code=409)
        return await _render(request, values=values, status_code=409, msg=ENCRYPTION_KEY_MESSAGE)
    if errors:
        if _wants_json(request):
            return JSONResponse({"ok": False, "errors": errors, "values": values}, status_code=422)
        return await _render(request, errors=errors, values=values, status_code=422)
    try:
        await _apply_fields(store, changes, reset_keys=reset_keys)
    except EncryptionKeyRequired:
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": {"code": "encryption_key_required", "message": ENCRYPTION_KEY_MESSAGE, "retryable": False}}, status_code=409)
        return await _render(request, values=values, status_code=409, msg=ENCRYPTION_KEY_MESSAGE)
    # Provider changes invalidate prior consent via configuration fingerprint;
    # blank secrets keep current values and are never echoed back.
    notice = "Studio settings saved."
    _flash(request, notice, "studio")
    if _wants_json(request):
        return await _canonical_settings_json(request, "studio", notice)
    return _redirect("studio")


@router.post("/research")
async def save_research(
    request: Request,
    research_enabled: str = Form(""),
    blocked_domains: str = Form(""),
):
    if (early := await _begin_write(request)) is not None:
        return early
    store = _store(request)
    assert store is not None  # _begin_write guarantees an available store
    errors: dict[str, str] = {}
    values = {"research_enabled": research_enabled, "blocked_domains": blocked_domains}
    changes = {
        "research.enabled": research_enabled == "1",
        "research.blocked_domains": blocked_domains,
    }
    _validate_field(store, "research.enabled", "research_enabled", changes["research.enabled"], errors)
    _validate_field(store, "research.blocked_domains", "blocked_domains", changes["research.blocked_domains"], errors)
    if errors:
        if _wants_json(request):
            return JSONResponse({"ok": False, "errors": errors, "values": values}, status_code=422)
        return await _render(request, errors=errors, values=values, status_code=422)
    await _apply_fields(store, changes)
    notice = "Research settings saved."
    _flash(request, notice, "research")
    if _wants_json(request):
        return await _canonical_settings_json(request, "research", notice)
    return _redirect("research")


def _reset_route(section: str):
    async def reset_setting(request: Request, key: str = Form("")):
        if (early := await _begin_write(request)) is not None:
            return early
        if not key.startswith(f"{section}."):
            if _wants_json(request):
                return JSONResponse({"ok": False, "error": {"code": "invalid_key", "message": "Unknown setting for this section.", "retryable": False}}, status_code=422)
            raise HTTPException(status_code=422, detail="Unknown setting for this section")
        reset_store = _store(request)
        assert reset_store is not None  # _begin_write guarantees an available store
        try:
            await reset_store.reset(key)
        except ValueError as exc:
            if _wants_json(request):
                return JSONResponse({"ok": False, "error": {"code": "invalid_key", "message": str(exc), "retryable": False}}, status_code=422)
            raise HTTPException(status_code=422, detail=str(exc)) from None
        notice = "Setting reset to the .env value."
        _flash(request, notice, section)
        if _wants_json(request):
            return await _canonical_settings_json(request, section, notice)
        return _redirect(section)

    reset_setting.__name__ = f"reset_{section}"
    return reset_setting


for _section in ("collection", "studio", "research"):
    router.add_api_route(f"/{_section}/reset", _reset_route(_section), methods=["POST"])


__all__ = ["router", "templates", "validate_channel_identifier"]


@router.get("/providers/models")
async def provider_models(request: Request, provider: str = Query(...), ollama_url: str | None = Query(None, max_length=300)):
    from ..studio.connections import available_models
    context = require_auth(request)
    if context.role != "owner":
        raise HTTPException(403, "Only the owner can manage providers")
    try:
        return {"models": await available_models(request.app.state.settings, provider, ollama_url=ollama_url)}
    except Exception:
        return JSONResponse({"error": {"message": "Models could not load. Check the connection and try again."}}, status_code=409)


@router.post("/providers/openai/connect")
async def connect_chatgpt(request: Request):
    from ..studio.connections import start_sign_in
    if (early := await _begin_write(request)) is not None:
        return early
    context = require_auth(request)
    # The official public-client OAuth flow requires a loopback callback.
    # Never derive its host from untrusted Host/forwarded headers.
    if request.url.hostname != "127.0.0.1" or request.url.scheme != "http":
        _flash(request, "ChatGPT sign-in requires opening TGhost on 127.0.0.1 on this computer.", "studio")
        return _redirect("studio")
    callback = f"http://127.0.0.1:{request.url.port or 8080}/auth/callback"
    try:
        url = await start_sign_in(_store(request), workspace_id=str(context.workspace_id), user_id=str(context.user_id), callback=callback)
        return RedirectResponse(url, status_code=303)
    except Exception:
        _flash(request, "ChatGPT sign-in could not start. Check encrypted settings storage.", "studio")
        return _redirect("studio")


@router.post("/providers/openai/disconnect")
async def disconnect_chatgpt(request: Request):
    if (early := await _begin_write(request)) is not None:
        return early
    from ..studio.connections import disconnect
    await disconnect(_store(request))
    _flash(request, "ChatGPT disconnected from TGhost.", "studio")
    return _redirect("studio")


async def chatgpt_callback(request: Request):
    from ..studio.connections import complete_sign_in
    # Uvicorn writes the query from scope on response. Remove authorization
    # codes before any response or auth error can reach its access logger.
    query = dict(request.query_params)
    request.scope["query_string"] = b""
    context = require_auth(request)
    if context.role != "owner":
        raise HTTPException(403, "Only the owner can connect ChatGPT")
    try:
        await complete_sign_in(_store(request), query, workspace_id=str(context.workspace_id), user_id=str(context.user_id))
        _flash(request, "ChatGPT connected. Choose an available model and save Studio settings.", "studio")
    except Exception:
        _flash(request, "ChatGPT sign-in did not complete. Start sign-in again; your previous connection is kept.", "studio")
    return RedirectResponse("/settings?provider=openai#studio", status_code=303)
