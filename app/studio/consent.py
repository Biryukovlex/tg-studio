"""Provider-consent helpers with configuration-bound, secret-free fingerprints."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .. import limits


CONSENT_VERSION = "m3.consent.v1"
PROVIDER_NAME = "openrouter"


def configuration_fingerprint(settings, *, provider: str = PROVIDER_NAME) -> str:
    """Hash only non-secret provider configuration used for an LLM request."""

    payload = {
        "consent_version": CONSENT_VERSION,
        "provider": provider,
        "model": str(getattr(settings, "openrouter_model", "") or "").strip() or "openai/gpt-4o-mini",
        "base_url": limits.OPENROUTER_BASE_URL,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def consent_required(settings) -> bool:
    """Test mode is local-only; every real OpenRouter request needs consent."""

    return not bool(getattr(settings, "studio_test_mode", False))


def disclosure(settings, *, fingerprint: str | None = None) -> dict[str, Any]:
    """Concise UI copy shown before the first external provider request."""

    return {
        "provider": PROVIDER_NAME,
        "configuration_fingerprint": fingerprint or configuration_fingerprint(settings),
        "title": "Allow OpenRouter for Studio",
        "message": "Studio will send bounded channel evidence, your instruction, and short conversation context to OpenRouter to help analyze and improve posts. Telegram session credentials and discussion comment bodies are never sent.",
        "required": consent_required(settings),
    }


def consent_state(settings, row: dict[str, Any] | None) -> dict[str, Any]:
    fingerprint = configuration_fingerprint(settings)
    granted = bool(row and row.get("allowed") and row.get("configuration_fingerprint") == fingerprint)
    required = consent_required(settings)
    return {
        "provider": PROVIDER_NAME,
        "configuration_fingerprint": fingerprint,
        "required": required,
        "granted": granted or not required,
        "granted_at": row.get("granted_at") if granted and row else None,
        "revoked_at": row.get("revoked_at") if row else None,
        "disclosure": disclosure(settings, fingerprint=fingerprint) if required and not granted else None,
    }


__all__ = ["CONSENT_VERSION", "PROVIDER_NAME", "configuration_fingerprint", "consent_required", "consent_state", "disclosure"]
