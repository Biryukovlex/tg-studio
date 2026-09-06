"""Studio setup-state calculation with no provider/network side effects."""

from __future__ import annotations

from .search_health import configured_search_state


def build_setup_state(settings, db) -> dict:
    blockers: list[dict[str, str]] = []
    if not settings.openrouter_api_key and not getattr(settings, "studio_test_mode", False):
        blockers.append({"code": "openrouter_key_missing", "message": "Add OPENROUTER_API_KEY to enable the agent."})
    if not getattr(db, "is_postgres", False) and not getattr(settings, "studio_test_mode", False):
        blockers.append({"code": "postgres_required", "message": "Set DATABASE_URL and migrate PostgreSQL before enabling Studio."})
    if getattr(db, "is_postgres", False) and not settings.telegram_session_encryption_key:
        blockers.append({"code": "session_key_missing", "message": "Set TELEGRAM_SESSION_ENCRYPTION_KEY for encrypted Telegram persistence."})
    if not settings.channel_list:
        blockers.append({"code": "channel_missing", "message": "Configure at least one Telegram channel."})
    return {
        "enabled": bool(settings.studio_enabled),
        "ready": bool(settings.studio_enabled and not blockers),
        "workspace_slug": getattr(db, "workspace_slug", settings.local_workspace_slug),
        "provider": "openrouter",
        "research": configured_search_state(settings).as_dict(),
        "blockers": blockers,
    }
