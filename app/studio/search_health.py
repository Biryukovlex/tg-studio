"""Bounded health/degraded-state reporting for the optional search service."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from .. import limits


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_endpoint(value: str) -> str:
    """Return a non-secret endpoint label for internal use (never exposed)."""

    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname
    try:
        port_value = parsed.port
    except ValueError:
        return ""
    port = f":{port_value}" if port_value else ""
    return f"{parsed.scheme}://{host}{port}"


def _endpoint_hash(value: str) -> str:
    """Short opaque hash of the endpoint for public exposure."""

    if not value:
        return ""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class SearchHealth:
    provider: str
    enabled: bool
    configured: bool
    available: bool | None
    degraded: bool
    endpoint: str
    endpoint_hash: str
    checked_at: str
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "enabled": self.enabled,
            "configured": self.configured,
            "available": self.available,
            "degraded": self.degraded,
            "endpoint_hash": self.endpoint_hash,
            "checked_at": self.checked_at,
            "message": self.message,
        }


def configured_search_state(settings) -> SearchHealth:
    """Build setup state without making a network request."""

    provider = limits.SEARCH_PROVIDER
    enabled = bool(getattr(settings, "studio_search_enabled", False))
    endpoint = _safe_endpoint(str(getattr(settings, "studio_search_base_url", "") or ""))
    configured = bool(endpoint) and provider == "searxng"
    if not enabled:
        message = "Web research is disabled; the collector is unaffected."
        degraded = False
        available: bool | None = None
    elif not configured:
        message = "Private SearXNG is not configured; research is in degraded mode."
        degraded = True
        available = False
    else:
        message = "Private SearXNG is configured; health has not been checked yet."
        degraded = False
        available = None
    return SearchHealth(
        provider=provider,
        enabled=enabled,
        configured=configured,
        available=available,
        degraded=degraded,
        endpoint=endpoint,
        endpoint_hash=_endpoint_hash(endpoint),
        checked_at=_now(),
        message=message,
    )


async def check_search_health(settings, *, transport=None) -> SearchHealth:
    """Perform one short, read-only SearXNG health probe.

    No request is made when search is disabled or the endpoint is malformed.
    Error details are intentionally reduced to a stable public message so
    private network topology and provider payloads never reach the browser.
    """

    state = configured_search_state(settings)
    if not state.enabled or not state.configured:
        return state
    timeout = limits.SEARCH_TIMEOUT_SECONDS
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            transport=transport,
        ) as client:
            response = await client.get(state.endpoint + "/healthz")
        if 200 <= response.status_code < 300:
            return replace(state, available=True, degraded=False, message="Private SearXNG is healthy.")
        return replace(state, available=False, degraded=True, message="Private SearXNG health check failed; research is degraded.")
    except (httpx.HTTPError, OSError, TimeoutError):
        return replace(state, available=False, degraded=True, message="Private SearXNG is unreachable; research is degraded.")
