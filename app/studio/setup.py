"""Studio setup-state calculation with no provider/network side effects."""

from __future__ import annotations

from .. import limits
from .model import provider_name
from .connections import oauth_record
from .search_health import configured_search_state


def build_setup_state(settings, db) -> dict:
    blockers: list[dict[str, str]] = []
    provider = provider_name(settings)
    if not getattr(settings, "studio_test_mode", False):
        if provider == "openrouter" and not settings.openrouter_api_key:
            blockers.append({"code": "openrouter_key_missing", "message": "Add OPENROUTER_API_KEY to enable the agent."})
        elif provider == "ollama" and not settings.ollama_model.strip():
            blockers.append({"code": "model_not_configured", "message": "Choose an installed Ollama model in Settings."})
        elif provider == "openai" and (not oauth_record(settings) or not settings.openai_model.strip()):
            blockers.append({"code": "provider_not_configured", "message": "Connect ChatGPT and choose a model in Settings."})
    if not settings.telegram_session_encryption_key:
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
        "provider": provider,
        "research": configured_search_state(settings).as_dict(),
        "blockers": blockers,
    }
