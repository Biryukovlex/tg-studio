"""Web admin panel: FastAPI + server-rendered Jinja2 templates.

Auth is a signed-cookie session (itsdangerous) behind a simple login form,
so it works both locally and behind a reverse proxy on Hetzner.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import secrets
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from ..collector import Collector
from ..config import Settings
from ..async_compat import maybe_await
from ..telegram_formatting import render_telegram_html
from .dependencies import csrf_token as shared_csrf_token
from .dependencies import require_auth as shared_require_auth
from .dependencies import require_csrf as shared_require_csrf
from .dependencies import WorkspaceContext
from ..studio.routes import _public_event_payload as _filter_event_payload
from ..studio.routes import build_router as build_studio_router
from ..studio.repository import MemoryStudioRepository, RunNotFound, StudioRepository
from ..studio.service import StudioService

log = logging.getLogger("web")

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

_ORDER_KEYS = {"date", "views", "reactions", "comments", "shares"}

# Login rate limiting: 5 failures per IP per 15 minutes.
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _prune_attempts(now: float) -> None:
    cutoff = now - _LOGIN_WINDOW_SECONDS
    for ip, stamps in list(_LOGIN_ATTEMPTS.items()):
        filtered = [t for t in stamps if t > cutoff]
        if filtered:
            _LOGIN_ATTEMPTS[ip] = filtered
        else:
            _LOGIN_ATTEMPTS.pop(ip, None)


def _is_rate_limited(ip: str) -> tuple[bool, int]:
    now = time.monotonic()
    _prune_attempts(now)
    stamps = _LOGIN_ATTEMPTS.get(ip, [])
    if len(stamps) >= _LOGIN_MAX_ATTEMPTS:
        oldest = min(stamps) if stamps else now
        retry_after = int(max(1, (_LOGIN_WINDOW_SECONDS - (now - oldest))))
        return True, retry_after
    return False, 0


def _record_failed_attempt(ip: str) -> None:
    now = time.monotonic()
    _prune_attempts(now)
    _LOGIN_ATTEMPTS.setdefault(ip, []).append(now)


def _clear_attempts(ip: str) -> None:
    _LOGIN_ATTEMPTS.pop(ip, None)


def _sanitize_csv_cell(value: object) -> object:
    if value is None:
        return ""
    text = str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def reset_login_rate_limiter() -> None:
    _LOGIN_ATTEMPTS.clear()


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings):
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'"
        )
        if self._settings.behind_tls:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


def _optional_positive_int(value: str | int | None) -> int | None:
    """Treat an empty HTML select value as no filter instead of a 422 error."""
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _history_window(value: str | int | None) -> tuple[int | None, str]:
    """Return (days, UI value); None represents the entire stored history."""
    raw = str(value or "14").strip().lower()
    if raw == "all":
        return None, "all"
    try:
        parsed = int(raw)
    except ValueError:
        parsed = 14
    parsed = max(3, min(parsed, 3650))
    return parsed, str(parsed)


def _num(v) -> str:
    try:
        return f"{int(v or 0):,}"
    except (TypeError, ValueError):
        return str(v)


def _dt(v) -> str:
    return str(v or "")[:16]


def _post_link(row) -> str:
    """Public t.me link when we can build one, else '#'."""
    identifier = (row["identifier"] or "").lstrip("@")
    chat_id = row["chat_id"] or 0
    if identifier and not identifier.isdigit() and not identifier.startswith("http"):
        return f"https://t.me/{identifier}/{row['message_id']}"
    if chat_id:
        return f"https://t.me/c/{chat_id}/{row['message_id']}"
    return "#"


def _comment_link(row) -> str:
    username = (row["discussion_username"] or "").lstrip("@")
    message_id = row["telegram_message_id"]
    if username:
        return f"https://t.me/{username}/{message_id}"
    chat_id = int(row["discussion_chat_id"] or 0)
    if chat_id:
        internal = str(abs(chat_id))
        if internal.startswith("100"):
            internal = internal[3:]
        return f"https://t.me/c/{internal}/{message_id}"
    return "#"


def _load_or_create_secret(settings: Settings) -> str:
    if settings.session_secret:
        return settings.session_secret
    secret_file = settings.data_path / ".secret"
    if secret_file.exists():
        key = secret_file.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = secrets.token_urlsafe(32)
    secret_file.write_text(key, encoding="utf-8")
    try:
        secret_file.chmod(0o600)
    except OSError:
        pass
    return key


def create_app(collector: Collector, settings: Settings) -> FastAPI:
    db = collector.db
    app = FastAPI(title="TG Studio", docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=_load_or_create_secret(settings),
        https_only=settings.behind_tls,
        same_site="lax",
    )
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    db_context = WorkspaceContext(
        user_id=getattr(db, "user_id", None)
        or uuid.uuid5(uuid.NAMESPACE_URL, f"telegram-stats:user:{settings.admin_username}"),
        workspace_id=getattr(db, "workspace_id", None),
        workspace_slug=getattr(db, "workspace_slug", "community"),
        role="owner",
    )
    app.state.workspace_context = db_context
    app.state.settings = settings
    app.state.db = db
    studio_repository = StudioRepository(db) if getattr(db, "is_postgres", False) else MemoryStudioRepository()
    app.state.studio_repository = studio_repository
    app.state.studio_service = StudioService(studio_repository, settings)

    templates.env.filters["num"] = _num
    templates.env.filters["dt"] = _dt
    templates.env.globals["app_name"] = "TG Studio"

    def render(request: Request, name: str, ctx: dict, status_code: int = 200):
        ctx.update({"request": request})
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def require_auth(request: Request) -> None:
        shared_require_auth(request)

    def csrf_token(request: Request) -> str:
        return shared_csrf_token(request)

    def require_csrf(request: Request) -> None:
        shared_require_csrf(request)

    # Keep auth/CSRF and service wiring in one production Studio router.
    app.state.csrf_token = csrf_token
    app.include_router(build_studio_router())

    # ---------------- auth ----------------

    @app.get("/healthz")
    async def healthz():
        """Unauthenticated process/readiness probe with no private payload."""
        check = getattr(db, "healthcheck", None)
        if check is not None:
            try:
                await maybe_await(check())
            except Exception as exc:  # noqa: BLE001 - health endpoint must be bounded
                log.warning("database readiness check failed: %s", type(exc).__name__)
                return Response(content='{"status":"not_ready"}', media_type="application/json", status_code=503)
        return {"status": "ok", "storage": "postgresql" if getattr(db, "is_postgres", False) else "sqlite"}

    @app.get("/login")
    async def login_form(request: Request):
        if request.session.get("auth"):
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", {"error": ""})

    @app.post("/login")
    async def login(request: Request, username: str = Form(""), password: str = Form("")):
        ip = _client_ip(request)
        limited, retry_after = _is_rate_limited(ip)
        if limited:
            return Response(
                content="Too many failed login attempts. Try again later.",
                status_code=429,
                headers={"Retry-After": str(retry_after)},
                media_type="text/plain",
            )
        ok_user = secrets.compare_digest(username.encode("utf-8"), settings.admin_username.encode("utf-8"))
        ok_pass = secrets.compare_digest(password.encode("utf-8"), settings.admin_password.encode("utf-8"))
        if not (ok_user and ok_pass):
            _record_failed_attempt(ip)
            log.warning("failed admin login from %s", ip)
            return render(request, "login.html", {"error": "Invalid username or password"}, 401)
        _clear_attempts(ip)
        request.session["auth"] = True
        if db_context.user_id is not None:
            request.session["user_id"] = str(db_context.user_id)
        request.session["workspace_slug"] = db_context.workspace_slug
        return RedirectResponse("/", status_code=303)

    @app.get("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ---------------- Studio compatibility alias ----------------

    @app.get("/studio-spike")
    async def studio_spike_compat(request: Request):
        """Redirect the M0 URL to the durable M2 Studio surface."""
        require_auth(request)
        if not settings.studio_enabled:
            raise HTTPException(status_code=404, detail="Studio is disabled")
        return RedirectResponse("/studio", status_code=307)

    @app.post("/studio-spike/api/agent")
    async def studio_spike_agent_compat(request: Request):
        """Accept one release cycle of clients that still use the M0 path."""
        require_auth(request)
        if not settings.studio_enabled:
            raise HTTPException(status_code=404, detail="Studio is disabled")
        require_csrf(request)
        return await app.state.studio_service.stream_request(request, await request.body())

    @app.post("/studio-spike/api/runs/{run_id}/cancel")
    async def studio_spike_cancel_compat(request: Request, run_id: str):
        require_auth(request)
        if not settings.studio_enabled:
            raise HTTPException(status_code=404, detail="Studio is disabled")
        require_csrf(request)
        try:
            parsed = uuid.UUID(run_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Run not found") from None
        try:
            run = await app.state.studio_service.cancel(parsed)
        except RunNotFound:
            raise HTTPException(status_code=404, detail="Run not found") from None
        status = "cancel_requested" if run["status"] in {"queued", "running"} else run["status"]
        return {"run_id": run_id, "status": status}

    @app.get("/studio-spike/api/runs/{run_id}/events")
    async def studio_spike_events_compat(request: Request, run_id: str, after: int = 0):
        require_auth(request)
        if not settings.studio_enabled:
            raise HTTPException(status_code=404, detail="Studio is disabled")
        try:
            parsed = uuid.UUID(run_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Run not found") from None
        run = await app.state.studio_repository.get_run(parsed)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        events = await app.state.studio_repository.get_events(parsed, after=after)
        return {
            "run_id": run_id,
            "thread_id": str(run["conversation_id"]),
            "events": [
                {
                    "type": event["event_type"],
                    "runId": run_id,
                    **_filter_event_payload(event.get("safe_payload", {})),
                }
                for event in events
            ],
        }

    # ---------------- pages ----------------

    @app.get("/")
    async def dashboard(
        request: Request,
        channel: str = "",
        order: str = "date",
        days: str = "14",
        msg: str = "",
    ):
        require_auth(request)
        channel_id = _optional_positive_int(channel)
        window_days, window_value = _history_window(days)
        channels = await maybe_await(db.get_channels())
        k = await maybe_await(db.kpis(channel_id))
        ts = await maybe_await(db.timeseries_totals(days=window_days, channel_id=channel_id))
        rows = await maybe_await(db.latest_stats(
            channel_id=channel_id, order=order if order in _ORDER_KEYS else "date"
        ))
        rows = [dict(r) | {"link": _post_link(r)} for r in rows]
        chart = {
            "labels": ts["days"],
            "views": ts["views"],
            "reactions": ts["reactions"],
            "comments": ts["comments"],
            "shares": ts["shares"],
            "postsPerDay": ts["posts_per_day"],
        }
        return render(request, "dashboard.html", {
            "channels": channels,
            "current_channel": channel_id,
            "order": order,
            "days": window_value,
            "msg": msg,
            "k": k,
            "rows": rows,
            "chart": chart,
        })

    @app.get("/post/{post_id}")
    async def post_detail(request: Request, post_id: int):
        require_auth(request)
        row = await maybe_await(db.post_row(post_id))
        if row is None:
            raise HTTPException(status_code=404, detail="Post not found")
        hist = await maybe_await(db.post_history(post_id))
        comments = [
            dict(comment) | {"link": _comment_link(comment)}
            for comment in await maybe_await(db.comments_for_post(post_id))
        ]
        row = dict(row)
        row["link"] = _post_link(row)
        row["formatted_html"] = render_telegram_html(
            row.get("text"), row.get("formatting_entities")
        )
        chart = {
            "labels": [_dt(h["taken_at"]) for h in hist],
            "views": [h["views"] for h in hist],
            "comments": [h["comments"] for h in hist],
            "reactions": [h["reactions"] for h in hist],
            "shares": [h["shares"] for h in hist],
        }
        return render(request, "post.html", {
            "row": row,
            "hist": list(reversed(hist)),
            "comments": comments,
            "chart": chart,
        })

    @app.post("/refresh")
    async def refresh(request: Request, channel: str = Form("")):
        require_auth(request)
        if settings.process_role.strip().lower() == "web":
            # In split deployments only the worker owns a Telegram session.
            # Keep the button harmless and explain where refresh work lives.
            return RedirectResponse("/?msg=Refresh+is+handled+by+the+collector+worker", status_code=303)
        asyncio.create_task(collector.poll_all(reason="web"))
        url = "/?msg=Refresh+started+-+numbers+will+update+shortly"
        channel_id = _optional_positive_int(channel)
        if channel_id:
            url += f"&channel={channel_id}"
        return RedirectResponse(url, status_code=303)

    @app.get("/export.csv")
    async def export_csv(request: Request, channel: str = ""):
        require_auth(request)
        rows = await maybe_await(db.latest_stats(channel_id=_optional_positive_int(channel), limit=100000))
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "post_db_id", "message_id", "channel", "posted_at_utc", "text",
            "views", "reactions", "comments", "shares", "updated_at_utc",
        ])
        for r in rows:
            writer.writerow([
                _sanitize_csv_cell(r["id"]), _sanitize_csv_cell(r["message_id"]), _sanitize_csv_cell(r["identifier"]), _sanitize_csv_cell(r["posted_at"]),
                _sanitize_csv_cell((r["text"] or "").replace("\n", " ")),
                _sanitize_csv_cell(r["views"]), _sanitize_csv_cell(r["reactions"]), _sanitize_csv_cell(r["comments"]), _sanitize_csv_cell(r["shares"]), _sanitize_csv_cell(r["updated_at"]),
            ])
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=tg-studio-export.csv"},
        )

    @app.get("/export-comments.csv")
    async def export_comments_csv(request: Request, channel: str = ""):
        require_auth(request)
        rows = await maybe_await(db.all_comments(channel_id=_optional_positive_int(channel)))
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "comment_db_id", "post_db_id", "post_message_id", "channel",
            "comment_message_id", "discussion_chat_id", "posted_at_utc",
            "edited_at_utc", "sender_id", "sender_name", "sender_username",
            "text", "media_type", "reactions", "reply_to_message_id", "deleted",
        ])
        for row in rows:
            writer.writerow([
                _sanitize_csv_cell(row["id"]), _sanitize_csv_cell(row["post_id"]), _sanitize_csv_cell(row["post_message_id"]),
                _sanitize_csv_cell(row["channel_identifier"]), _sanitize_csv_cell(row["telegram_message_id"]),
                _sanitize_csv_cell(row["discussion_chat_id"]), _sanitize_csv_cell(row["posted_at"]), _sanitize_csv_cell(row["edited_at"]),
                _sanitize_csv_cell(row["sender_id"]), _sanitize_csv_cell(row["sender_name"]), _sanitize_csv_cell(row["sender_username"]),
                _sanitize_csv_cell((row["text"] or "").replace("\n", " ")), _sanitize_csv_cell(row["media_type"]),
                _sanitize_csv_cell(row["reactions"]), _sanitize_csv_cell(row["reply_to_message_id"]), _sanitize_csv_cell(row["is_deleted"]),
            ])
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=tg-comments-export.csv"
            },
        )

    return app
