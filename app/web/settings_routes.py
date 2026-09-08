"""Owner-only workspace settings page (``/settings``).

Plain HTML forms post to one route per card. Values live in the per-workspace
store (``app.workspace_settings``); channels and the Telegram connection are
managed directly in their own tables. Secrets are never rendered back.
"""

from __future__ import annotations

import inspect
import re
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import limits
from ..async_compat import maybe_await
from ..session_crypto import build_cipher
from ..studio.search_health import configured_search_state
from ..studio.setup import build_setup_state
from ..workspace_settings import SETTINGS, EncryptionKeyRequired, StoreUnavailable, WorkspaceSettings, format_timestamp
from .dependencies import require_auth

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

ENCRYPTION_KEY_MESSAGE = "Set TELEGRAM_SESSION_ENCRYPTION_KEY to store secrets."
SYSTEM_PROMPT_MAX_CHARS = 12_000

_FRIENDLY_ERRORS = {
    "poll_minutes": "Poll interval must be between 1 and 1440 minutes.",
    "track_days": "History window must be between 0 and 3650 days.",
    "backfill_limit": "Backfill limit must be between 0 and 100000 posts.",
    "model": "Model must be 1 to 200 characters without spaces.",
    "openrouter_api_key": "API key must be at most 512 characters.",
    "blocked_domains": "Blocked domains must be comma separated hostnames.",
}

_CHANNEL_MESSAGE = "Channel identifier must be @name, t.me/name, or -100…"
_USERNAME_RE = re.compile(r"[A-Za-z0-9_]{5,32}")


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


def _flash(request: Request, message: str) -> None:
    request.session["flash_msg"] = message


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


async def _system_prompt(request: Request) -> str:
    repository = getattr(request.app.state, "studio_repository", None)
    getter = getattr(repository, "get_system_prompt", None)
    if getter is None:
        return ""
    try:
        value = getter()
        if inspect.isawaitable(value):
            value = await value
    except Exception:  # noqa: BLE001 - the page must render even if Studio storage is unavailable
        return ""
    return value if isinstance(value, str) else ""


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
    if db is not None and hasattr(db, "get_channels"):
        try:
            channels = [dict(row) for row in await maybe_await(db.get_channels())]
        except Exception:  # noqa: BLE001 - render the page even when the channel query fails
            channels = []

    connection = {"configured": False, "api_id": None, "has_session": False, "updated_at": None}
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
    return {
        "request": request,
        "channels": channels,
        "connection_status": connection,
        "ws_dict": ws_dict,
        "available": _available(request),
        "errors": errors or {},
        "values": values or {},
        "msg": flash,
        "can_manage_settings": _is_owner(request),
        "setup_state": setup_state,
        "search_state": search_state,
        "search_base_url": str(getattr(settings, "studio_search_base_url", "") or "").strip(),
        "system_prompt": await _system_prompt(request),
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
        return redirect
    return await _render(request)


async def _begin_write(request: Request) -> RedirectResponse | HTMLResponse | None:
    """Shared prelude for every POST: auth, owner, CSRF, store availability."""
    if (redirect := _guard(request)) is not None:
        return redirect
    await _require_csrf(request)
    if not _available(request):
        return await _render(request)
    return None


@router.post("/channels/add")
async def add_channel(request: Request, identifier: str = Form("")):
    if (early := await _begin_write(request)) is not None:
        return early
    error = validate_channel_identifier(identifier)
    if error is None:
        try:
            await request.app.state.db.add_channel(identifier.strip())
        except ValueError as exc:
            error = str(exc)
    if error is not None:
        return await _render(request, errors={"identifier": error}, values={"identifier": identifier}, status_code=422)
    _flash(request, f"Channel {identifier.strip()} added.")
    return _redirect("telegram")


@router.post("/channels/{channel_id}/deactivate")
async def deactivate_channel(request: Request, channel_id: int):
    if (early := await _begin_write(request)) is not None:
        return early
    db = request.app.state.db
    label = f"#{channel_id}"
    try:
        for row in await maybe_await(db.get_channels()):
            if int(row["id"]) == channel_id:
                label = row["identifier"]
                break
    except Exception:  # noqa: BLE001 - the label is cosmetic
        pass
    await db.deactivate_channel(channel_id)
    _flash(request, f"Channel {label} deactivated.")
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
        return await _render(request, errors=errors, values=values, status_code=422)

    cipher = build_cipher(str(getattr(settings, "telegram_session_encryption_key", "") or ""))
    if cipher is None:
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
        return await _render(request, errors=errors, values=values, status_code=422)

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
    _flash(request, "Connection saved. Restart the collector to use the updated credentials.")
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
    errors: dict[str, str] = {}
    values = {"poll_minutes": poll_minutes, "track_days": track_days, "backfill_limit": backfill_limit}
    changes: dict[str, Any] = {}
    try:
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
    except StoreUnavailable:
        return await _render(request)
    if errors:
        return await _render(request, errors=errors, values=values, status_code=422)
    await _apply_fields(store, changes)
    _flash(request, "Collection settings saved.")
    return _redirect("collection")


@router.post("/studio")
async def save_studio(
    request: Request,
    openrouter_api_key: str = Form(""),
    clear_openrouter_api_key: str = Form(""),
    model: str = Form(""),
    system_prompt: str = Form(""),
):
    if (early := await _begin_write(request)) is not None:
        return early
    store = _store(request)
    errors: dict[str, str] = {}
    values: dict[str, Any] = {"model": model, "system_prompt": system_prompt}
    changes: dict[str, Any] = {}
    reset_keys: tuple[str, ...] = ()
    try:
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
        else:
            errors["model"] = "Model is required."
    except EncryptionKeyRequired:
        return await _render(request, values=values, status_code=409, msg=ENCRYPTION_KEY_MESSAGE)
    except StoreUnavailable:
        return await _render(request)
    if len(system_prompt) > SYSTEM_PROMPT_MAX_CHARS:
        errors["system_prompt"] = f"System prompt must be at most {SYSTEM_PROMPT_MAX_CHARS} characters."
    if errors:
        return await _render(request, errors=errors, values=values, status_code=422)
    repository = getattr(request.app.state, "studio_repository", None)
    setter = getattr(repository, "set_system_prompt", None)
    previous_prompt = await _system_prompt(request)
    if setter is not None:
        try:
            await maybe_await(setter(system_prompt))
        except Exception:  # noqa: BLE001 - surface as a field error, never a 500
            return await _render(
                request,
                errors={"system_prompt": "Could not save the system prompt."},
                values=values,
                status_code=422,
            )
    try:
        await _apply_fields(store, changes, reset_keys=reset_keys)
    except (EncryptionKeyRequired, StoreUnavailable):
        if setter is not None:
            try:
                await maybe_await(setter(previous_prompt))
            except Exception:  # noqa: BLE001 - preserve the original storage error
                pass
        if not store.available:
            return await _render(request)
        return await _render(request, values=values, status_code=409, msg=ENCRYPTION_KEY_MESSAGE)
    except Exception:
        if setter is not None:
            try:
                await maybe_await(setter(previous_prompt))
            except Exception:  # noqa: BLE001 - preserve the original storage error
                pass
        raise
    _flash(request, "Studio settings saved.")
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
    errors: dict[str, str] = {}
    values = {"research_enabled": research_enabled, "blocked_domains": blocked_domains}
    changes = {
        "research.enabled": research_enabled == "1",
        "research.blocked_domains": blocked_domains,
    }
    try:
        _validate_field(store, "research.enabled", "research_enabled", changes["research.enabled"], errors)
        _validate_field(store, "research.blocked_domains", "blocked_domains", changes["research.blocked_domains"], errors)
    except StoreUnavailable:
        return await _render(request)
    if errors:
        return await _render(request, errors=errors, values=values, status_code=422)
    await _apply_fields(store, changes)
    _flash(request, "Research settings saved.")
    return _redirect("research")


def _reset_route(section: str):
    async def reset_setting(request: Request, key: str = Form("")):
        if (early := await _begin_write(request)) is not None:
            return early
        if not key.startswith(f"{section}."):
            raise HTTPException(status_code=422, detail="Unknown setting for this section")
        try:
            await _store(request).reset(key)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except StoreUnavailable:
            return await _render(request)
        _flash(request, "Setting reset to the .env value.")
        return _redirect(section)

    reset_setting.__name__ = f"reset_{section}"
    return reset_setting


for _section in ("collection", "studio", "research"):
    router.add_api_route(f"/{_section}/reset", _reset_route(_section), methods=["POST"])


__all__ = ["router", "templates", "validate_channel_identifier"]
