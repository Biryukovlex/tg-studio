"""Studio setup-state calculation with no provider/network side effects."""

from __future__ import annotations

from .. import limits
from .search_health import configured_search_state


def build_setup_state(settings, db) -> dict:
    blockers: list[dict[str, str]] = []
    if not settings.openrouter_api_key and not getattr(settings, "studio_test_mode", False):
        blockers.append({"code": "openrouter_key_missing", "message": "Add OPENROUTER_API_KEY to enable the agent."})
    if not getattr(db, "is_postgres", False) and not getattr(settings, "studio_test_mode", False):
        blockers.append({"code": "postgres_required", "message": "Set DATABASE_URL and migrate PostgreSQL before enabling Studio."})
    if getattr(db, "is_postgres", False) and not settings.telegram_session_encryption_key:
        blockers.append({"code": "session_key_missing", "message": "Set TELEGRAM_SESSION_ENCRYPTION_KEY for encrypted Telegram persistence."})
    # Channels may come from .env or from rows added on the settings page; the
    # database keeps a count refreshed on every channel read.
    db_count = getattr(db, "active_channel_count", None)
    has_channels = bool(settings.channel_list) or bool(db_count)
    if not has_channels:
        blockers.append({"code": "channel_missing", "message": "Add at least one Telegram channel in Settings."})
    return {
        "ready": bool(not blockers),
        "workspace_slug": getattr(db, "workspace_slug", limits.WORKSPACE_SLUG),
        "provider": "openrouter",
        "research": configured_search_state(settings).as_dict(),
        "blockers": blockers,
    }
