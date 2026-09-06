"""Privacy-safe run observability helpers.

The Studio deliberately keeps the provider boundary small.  This module is the
single place where provider usage and exception objects become data that may be
persisted or emitted to logs.  Callers should pass the returned projections,
never the original provider response or exception string.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from .model import StudioConfigurationError


_MAX_COUNT = 1_000_000_000
_MAX_COST_USD = Decimal("1000000000")
_SAFE_DETAIL = re.compile(r"^[a-z][a-z0-9_]{0,48}_tokens$")

# The message is intentionally independent of the exception text.  A provider
# can include request headers, URLs, prompts, or response bodies in an error.
_ERRORS: dict[str, tuple[str, bool]] = {
    "studio_not_configured": ("Studio is not configured for an agent run.", False),
    "provider_consent_required": ("Review and confirm the provider disclosure before using Studio.", False),
    "provider_timeout": ("The agent took too long to respond. Try again.", True),
    "provider_rate_limited": ("The model provider is busy. Try again shortly.", True),
    "provider_unavailable": ("The model provider is unavailable. Try again shortly.", True),
    "provider_invalid_response": ("The model provider returned an invalid response. Try again.", True),
    "invalid_agent_output": ("The agent returned an invalid response. Try again.", True),
    "run_cancelled": ("Run cancelled by the user.", False),
    "run_interrupted": ("The worker stopped before completion.", True),
    "agent_failed": ("The agent could not complete this run. Try again.", True),
}


def _bounded_int(value: Any, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, min(number, _MAX_COUNT))


def _bounded_money(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount < 0 or amount > _MAX_COST_USD:
        return None
    return float(round(amount, 8))


def _value(source: Any, *names: str) -> Any:
    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                return source[name]
        return None
    for name in names:
        value = getattr(source, name, None)
        if value is not None:
            return value
    return None


def normalize_usage(value: Any = None, *, latency_ms: int | None = None) -> dict[str, Any]:
    """Return a bounded, JSON-safe usage projection.

    PydanticAI exposes ``RunUsage`` while test seams and future providers may
    return dictionaries.  Only token/request counters and a best-effort USD
    amount cross this boundary; arbitrary provider metadata is discarded.
    """

    source = value if value is not None else {}
    input_tokens = _bounded_int(_value(source, "input_tokens", "prompt_tokens", "request_tokens"))
    output_tokens = _bounded_int(_value(source, "output_tokens", "completion_tokens", "response_tokens"))
    explicit_total = _value(source, "total_tokens")
    total_tokens = _bounded_int(explicit_total, default=input_tokens + output_tokens) if explicit_total is not None else min(input_tokens + output_tokens, _MAX_COUNT)
    if total_tokens < input_tokens + output_tokens:
        total_tokens = min(input_tokens + output_tokens, _MAX_COUNT)

    usage: dict[str, Any] = {
        "requests": _bounded_int(_value(source, "requests", "request_count")),
        "tool_calls": _bounded_int(_value(source, "tool_calls")),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    for key in (
        "cache_read_tokens",
        "cache_write_tokens",
        "input_audio_tokens",
        "cache_audio_read_tokens",
        "output_audio_tokens",
    ):
        count = _bounded_int(_value(source, key))
        if count:
            usage[key] = count

    details = _value(source, "details")
    if isinstance(details, Mapping):
        safe_details: dict[str, int] = {}
        for key, item in details.items():
            name = str(key)
            if _SAFE_DETAIL.fullmatch(name):
                count = _bounded_int(item)
                if count:
                    safe_details[name] = count
        if safe_details:
            usage["details"] = dict(sorted(safe_details.items())[:16])

    cost = _bounded_money(_value(source, "estimated_cost_usd", "cost", "estimated_cost"))
    if cost is not None:
        usage["estimated_cost_usd"] = cost

    if latency_ms is not None:
        usage["latency_ms"] = _bounded_int(latency_ms)
    return usage


def duration_ms(started_at: Any, finished_at: Any = None) -> int | None:
    """Calculate a bounded duration from persisted timestamps."""

    if started_at is None:
        return None
    end = finished_at
    if end is None:
        from datetime import datetime, timezone

        end = datetime.now(timezone.utc)
    try:
        seconds = (end - started_at).total_seconds()
    except (AttributeError, TypeError):
        return None
    if not math.isfinite(seconds):
        return None
    return max(0, min(int(seconds * 1000), _MAX_COUNT))


def safe_error(exc: BaseException) -> tuple[str, str, bool]:
    """Classify an exception without retaining its message or payload."""

    if isinstance(exc, StudioConfigurationError):
        return "studio_not_configured", _ERRORS["studio_not_configured"][0], False
    if isinstance(exc, asyncio.CancelledError):
        return "run_cancelled", _ERRORS["run_cancelled"][0], False
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "provider_timeout", _ERRORS["provider_timeout"][0], True
    name = exc.__class__.__name__.lower()
    text = name + " " + str(exc).lower()
    if any(token in text for token in ("rate", "quota", "429")):
        code = "provider_rate_limited"
    elif any(token in text for token in ("timeout", "timed out")):
        code = "provider_timeout"
    elif any(token in text for token in ("connect", "connection", "unavailable", "503", "502", "bad gateway")):
        code = "provider_unavailable"
    elif isinstance(exc, ValidationError):
        code = "invalid_agent_output"
    elif any(token in name for token in ("unexpectedmodelbehavior", "usagelimit", "toolretry")):
        code = "invalid_agent_output"
    elif isinstance(exc, ValueError) or "invalid" in name:
        # Preserve the existing Studio contract for validation/parsing errors;
        # the message remains generic and never includes the exception text.
        code = "invalid_agent_output"
    else:
        code = "agent_failed"
    message, retryable = _ERRORS[code]
    return code, message, retryable


def safe_error_for_code(code: Any, fallback: str | None = None) -> tuple[str, str, bool]:
    """Normalize a persisted error code and return its public message."""

    normalized = str(code or "").strip().lower()
    if normalized not in _ERRORS:
        normalized = "agent_failed" if normalized else ""
    if not normalized:
        return "", "", False
    message, retryable = _ERRORS[normalized]
    return normalized, message, retryable


def emit_observation(logger: Any, kind: str, **fields: Any) -> None:
    """Emit a structured record containing only pre-projected metadata."""

    allowed = {
        "conversation_id",
        "run_id",
        "tool_name",
        "event_type",
        "status",
        "stage",
        "duration_ms",
        "result_count",
        "source_count",
        "story_count",
        "cache_hit",
        "degraded",
        "provider",
        "requested_model",
        "actual_model",
        "prompt_version",
        "analysis_id",
        "story_cluster_id",
        "draft_id",
        "error_code",
        "usage",
    }
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        if key == "usage":
            safe[key] = normalize_usage(value)
        elif key.endswith("_id") or key in {"provider", "requested_model", "actual_model", "prompt_version", "tool_name", "event_type", "stage", "error_code"}:
            safe[key] = str(value)[:160]
        elif key in {"cache_hit", "degraded"}:
            safe[key] = bool(value)
        else:
            safe[key] = _bounded_int(value)
    # ``extra`` is visible to JSON-capable logging handlers without forcing a
    # particular production logging formatter.  The message itself is generic.
    logger.info("studio.%s", str(kind)[:40], extra={"studio_observation": safe})
