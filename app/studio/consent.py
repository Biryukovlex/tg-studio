"""Provider-consent helpers with configuration-bound, secret-free fingerprints."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .. import limits
from .model import provider_name, model_name


CONSENT_VERSION = "m3.consent.v1"
PROVIDER_NAME = "openrouter"


def configuration_fingerprint(settings, *, provider: str | None = None) -> str:
    """Hash only non-secret provider configuration used for an LLM request."""

    provider = provider or provider_name(settings)
    payload = {
        "consent_version": CONSENT_VERSION,
        "provider": provider,
        "model": model_name(settings),
        "base_url": limits.OPENROUTER_BASE_URL if provider == "openrouter" else ("https://api.openai.com/v1" if provider == "openai" else settings.ollama_base_url),
    }
    if provider == "openai":
        from .connections import oauth_record
        account = oauth_record(settings)
        payload["account"] = account.get("subject", "")
        payload["client"] = account.get("client_id", "")
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def consent_required(settings) -> bool:
    """Local models require no external-provider consent."""

    return provider_name(settings) != "ollama" and not bool(getattr(settings, "studio_test_mode", False))


def disclosure(settings, *, fingerprint: str | None = None) -> dict[str, Any]:
    """Concise UI copy shown before the first external provider request."""

    label = "OpenAI (ChatGPT)" if provider_name(settings) == "openai" else "OpenRouter"
    return {
        "provider": provider_name(settings),
        "configuration_fingerprint": fingerprint or configuration_fingerprint(settings),
        "title": f"Allow {label} for Studio",
        "message": f"Studio will send bounded channel evidence, your instruction, and short conversation context to {label} to help analyze and improve posts. Telegram session credentials and discussion comment bodies are never sent.",
        "required": consent_required(settings),
    }


def consent_state(settings, row: dict[str, Any] | None) -> dict[str, Any]:
    fingerprint = configuration_fingerprint(settings)
    granted = bool(row and row.get("allowed") and row.get("configuration_fingerprint") == fingerprint)
    required = consent_required(settings)
    return {
        "provider": provider_name(settings),
        "configuration_fingerprint": fingerprint,
        "required": required,
        "granted": granted or not required,
        "granted_at": row.get("granted_at") if granted and row else None,
        "revoked_at": row.get("revoked_at") if row else None,
        "disclosure": disclosure(settings, fingerprint=fingerprint) if required and not granted else None,
    }


__all__ = ["CONSENT_VERSION", "PROVIDER_NAME", "configuration_fingerprint", "consent_required", "consent_state", "disclosure"]
