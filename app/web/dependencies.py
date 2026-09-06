"""Reusable authentication and workspace dependencies for FastAPI routes."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass

from fastapi import HTTPException, Request


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    """The only tenant context a request is allowed to use."""

    user_id: uuid.UUID | None
    workspace_id: uuid.UUID | None
    workspace_slug: str
    role: str

    @property
    def is_authenticated(self) -> bool:
        return self.user_id is not None


def _state_context(request: Request) -> WorkspaceContext:
    context = getattr(request.app.state, "workspace_context", None)
    if isinstance(context, WorkspaceContext):
        return context
    # SQLite compatibility mode has no tenant row yet.  It still gets an
    # explicit community boundary so callers cannot omit scope accidentally.
    return WorkspaceContext(
        user_id=None,
        workspace_id=None,
        workspace_slug="community",
        role="owner",
    )


def _wants_json(request: Request) -> bool:
    path = request.url.path or ""
    if path.startswith("/studio/api/") or path.startswith("/studio-spike/api/"):
        return True
    accept = request.headers.get("accept", "")
    # Prefer JSON when Accept explicitly prefers it over HTML.
    if "application/json" in accept:
        # If both json and html present, check q weights or order.
        # Simple heuristic: json before html or json with higher q.
        json_pos = accept.find("application/json")
        html_pos = accept.find("text/html")
        if html_pos == -1:
            return True
        return json_pos < html_pos
    return False


def require_workspace_context(request: Request) -> WorkspaceContext:
    """Return the authenticated request context or redirect to login."""
    if not request.session.get("auth"):
        if _wants_json(request):
            raise HTTPException(
                status_code=401,
                detail={"error": {"code": "unauthenticated", "message": "Sign in to continue.", "retryable": False}},
            )
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    base = _state_context(request)
    raw_user_id = request.session.get("user_id")
    user_id = base.user_id
    if raw_user_id:
        try:
            user_id = uuid.UUID(str(raw_user_id))
        except (TypeError, ValueError):
            raise HTTPException(status_code=401, detail="Invalid user session") from None
    return WorkspaceContext(
        user_id=user_id,
        workspace_id=base.workspace_id,
        workspace_slug=base.workspace_slug,
        role=base.role,
    )


def require_auth(request: Request) -> WorkspaceContext:
    """Compatibility name for existing routes during dependency extraction."""
    return require_workspace_context(request)


def csrf_token(request: Request) -> str:
    token = request.session.get("studio_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["studio_csrf_token"] = token
    return token


def require_csrf(request: Request) -> None:
    expected = request.session.get("studio_csrf_token")
    supplied = request.headers.get("x-csrf-token", "")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


def assert_workspace_id(context: WorkspaceContext, candidate: uuid.UUID | str | None) -> None:
    """Fail closed when an API/client attempts to select another workspace."""
    if candidate is None or context.workspace_id is None:
        return
    try:
        parsed = uuid.UUID(str(candidate))
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail="Workspace not found") from None
    if parsed != context.workspace_id:
        raise HTTPException(status_code=404, detail="Workspace not found")
