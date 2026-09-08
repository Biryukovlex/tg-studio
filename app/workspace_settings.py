"""Per-workspace settings store with encrypted secrets and env fallback."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("workspace_settings")

# Errors exposed to callers / HTTP mapping
def format_timestamp(value: Any) -> str | None:
    """Render a stored timestamp as ``YYYY-MM-DD HH:MM`` (UTC) for templates."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)[:16]


class EncryptionKeyRequired(RuntimeError):
    """Raised when a secret is saved without a cipher."""

class StoreUnavailable(RuntimeError):
    """Raised when the store is unavailable (SQLite mode)."""


# Registry of 7 workspace-scoped keys
# Each entry: key -> (type_name, env_attr, validator)
# Validators raise ValueError with key name on failure and return normalized value.
def _validate_poll_minutes(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError("collection.poll_minutes must be a number")
    if not (1 <= x <= 1440):
        raise ValueError("collection.poll_minutes must be between 1 and 1440")
    return float(x)


def _validate_track_days(v: Any) -> int:
    try:
        x = int(v)
    except (TypeError, ValueError):
        raise ValueError("collection.track_days must be an integer")
    if not (0 <= x <= 3650):
        raise ValueError("collection.track_days must be between 0 and 3650")
    return int(x)


def _validate_backfill_limit(v: Any) -> int:
    try:
        x = int(v)
    except (TypeError, ValueError):
        raise ValueError("collection.backfill_limit must be an integer")
    if not (0 <= x <= 100000):
        raise ValueError("collection.backfill_limit must be between 0 and 100000")
    return int(x)


def _validate_openrouter_key(v: Any) -> str:
    s = str(v or "")
    if len(s) > 512:
        raise ValueError("studio.openrouter_api_key must be at most 512 characters")
    return s


def _validate_model(v: Any) -> str:
    s = str(v or "").strip()
    if not (1 <= len(s) <= 200):
        raise ValueError("studio.model must be between 1 and 200 characters")
    if re.search(r"\s", s):
        raise ValueError("studio.model must not contain whitespace")
    return s


def _validate_research_enabled(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        low = v.strip().lower()
        if low in {"true", "1", "yes", "on"}:
            return True
        if low in {"false", "0", "no", "off", ""}:
            return False
    return bool(v)


def _validate_blocked_domains(v: Any) -> str:
    raw = str(v or "")
    # Split by comma, normalize each hostname
    parts = []
    for part in raw.split(","):
        p = part.strip().lower().lstrip(".")
        if not p:
            continue
        # Basic hostname validation: letters, digits, hyphen, dot
        if not re.fullmatch(r"[a-z0-9.-]{1,253}", p):
            raise ValueError(f"research.blocked_domains contains invalid hostname: {p}")
        # Must contain a dot or be single label? Allow single label for test
        parts.append(p)
    # Normalize to comma without spaces, deduplicate preserving order
    seen = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return ",".join(seen)


SETTINGS: dict[str, tuple[str, str, Any]] = {
    "collection.poll_minutes": ("float", "poll_minutes", _validate_poll_minutes),
    "collection.track_days": ("int", "track_days", _validate_track_days),
    "collection.backfill_limit": ("int", "backfill_limit", _validate_backfill_limit),
    "studio.openrouter_api_key": ("secret", "openrouter_api_key", _validate_openrouter_key),
    "studio.model": ("str", "openrouter_model", _validate_model),
    "research.enabled": ("bool", "studio_search_enabled", _validate_research_enabled),
    "research.blocked_domains": ("str", "studio_search_blocked_domains", _validate_blocked_domains),
}

# Mapping from registry key to Settings attribute name for RuntimeSettings proxy
KEY_TO_ATTR = {
    "collection.poll_minutes": "poll_minutes",
    "collection.track_days": "track_days",
    "collection.backfill_limit": "backfill_limit",
    "studio.openrouter_api_key": "openrouter_api_key",
    "studio.model": "openrouter_model",
    "research.enabled": "studio_search_enabled",
    "research.blocked_domains": "studio_search_blocked_domains",
}

ATTR_TO_KEY = {v: k for k, v in KEY_TO_ATTR.items()}


class RuntimeSettings:
    """Thin proxy over Settings that overlays DB values."""

    def __init__(self, base, workspace_settings: "WorkspaceSettings | None" = None, overlay: dict[str, Any] | None = None):
        # overlay is key -> decrypted/normalized value for keys present in DB
        # For dynamic reads, keep reference to workspace_settings
        self._base = base
        self._workspace_settings = workspace_settings
        self._overlay = overlay or {}

    def __getattr__(self, name: str) -> Any:
        # If we have a live workspace_settings, check its current rows
        if self._workspace_settings is not None:
            key = ATTR_TO_KEY.get(name)
            if key is not None and key in self._workspace_settings._rows:
                return self._workspace_settings._rows[key]["value"]
        # Fallback to static overlay (for tests that pass overlay directly with attr names)
        if name in self._overlay:
            return self._overlay[name]
        # Also handle overlay with registry keys (for backward compat)
        key = ATTR_TO_KEY.get(name)
        if key is not None and key in self._overlay:
            return self._overlay[key]
        # Otherwise delegate to base Settings
        return getattr(self._base, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"_base", "_workspace_settings", "_overlay"}:
            super().__setattr__(name, value)
        else:
            # Setting on proxy delegates to base (used rarely)
            setattr(self._base, name, value)

    def __repr__(self) -> str:
        ws_keys = list(self._workspace_settings._rows.keys()) if self._workspace_settings else []
        overlay_keys = list(self._overlay.keys())
        return f"<RuntimeSettings base={self._base!r} ws_keys={ws_keys!r} overlay_keys={overlay_keys!r}>"


class WorkspaceSettings:
    """Accessor for per-workspace settings with env fallback and secret handling."""

    def __init__(self, db, base, cipher):
        self._db = db
        self._base = base
        self._cipher = cipher
        # Internal state: key -> (value, is_secret, updated_at)
        self._rows: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self.telegram_restart_required = False
        # Determine availability: postgres with workspace_id vs sqlite
        self.available = bool(getattr(db, "is_postgres", False) and getattr(db, "workspace_id", None) is not None)
        # For SQLite mode, available is False; for postgres, True after init
        # If db is postgres but workspace_id not yet set (before init_db), we treat as available after load will check

    async def load(self) -> None:
        """Load all rows for this workspace into memory."""
        # Check if db is postgres and has workspace
        is_pg = bool(getattr(self._db, "is_postgres", False))
        if not is_pg:
            self.available = False
            self._rows = {}
            self._loaded = True
            return
        # If workspace_id is None, try to ensure workspace (for tests, db may not have init yet)
        ws_id = getattr(self._db, "workspace_id", None)
        if ws_id is None:
            # Try to call ensure_workspace or init_db? For tests, they may have not called init_db yet.
            # We treat as not available until init, but for T22 tests with memory db, they may use a fake db that doesn't have workspace.
            # In that case, we treat as available=False? But spec says SQLite mode is no-op, postgres mode is available.
            # For memory tests, db may be a fake with no postgres flag, so available False.
            # For postgres tests, workspace_id will be set after init_db.
            self.available = False
            self._rows = {}
            self._loaded = True
            return
        self.available = True
        # Fetch rows
        try:
            # Use db._execute or direct query
            # We support both PostgresDatabase and fake test DB
            if hasattr(self._db, "_execute"):
                result = await self._db._execute(
                    "SELECT key, value, is_secret, updated_at FROM workspace_settings WHERE workspace_id=:workspace_id",
                    {},
                )
                rows = list(result.mappings().all()) if hasattr(result, "mappings") else []
                new_rows: dict[str, dict[str, Any]] = {}
                for row in rows:
                    # row may be dict-like
                    d = dict(row) if not isinstance(row, dict) else row
                    key = d["key"]
                    val = d["value"]
                    is_secret = bool(d["is_secret"])
                    updated_at = d.get("updated_at")
                    # Decrypt if secret
                    if is_secret:
                        if self._cipher is not None:
                            try:
                                # val is stored as Fernet token string
                                token = val.encode("utf-8") if isinstance(val, str) else bytes(val)
                                decrypted = self._cipher.decrypt(token)
                                new_rows[key] = {"value": decrypted, "is_secret": True, "updated_at": updated_at, "raw": val}
                            except Exception:
                                # If decrypt fails, treat as not set
                                continue
                        else:
                            # No cipher, cannot decrypt, skip
                            continue
                    else:
                        # Convert stored string back to typed value via validator
                        try:
                            typ, _, validator = SETTINGS.get(key, ("str", "", lambda x: x))
                            if typ == "bool":
                                typed = validator(val) if isinstance(val, str) else bool(val)
                            elif typ in ("int", "float", "str"):
                                # Use validator to normalize
                                typed = validator(val)
                            else:
                                typed = val
                        except Exception:
                            typed = val
                        new_rows[key] = {"value": typed, "is_secret": False, "updated_at": updated_at, "raw": val}
                self._rows = new_rows
            else:
                # Fake DB for tests: expect it to have a dict
                self._rows = {}
        except Exception:
            # Fail closed: keep the last known rows rather than silently dropping overrides.
            log.exception("workspace settings load failed; keeping %d cached rows", len(self._rows))
        self._loaded = True

    def validate(self, key: str, value: Any) -> Any:
        """Validate one value without changing memory or persistent state."""
        if not self.available:
            raise StoreUnavailable("Settings store is not available in SQLite mode")
        if key not in SETTINGS:
            raise ValueError(f"unknown settings key: {key}")
        typ, _env_attr, validator = SETTINGS[key]
        try:
            normalized = validator(value)
        except ValueError as e:
            msg = str(e)
            if key not in msg:
                raise ValueError(f"{key}: {msg}") from None
            raise
        if typ == "secret":
            if normalized == "":
                raise ValueError(f"{key} must be reset instead of saved empty")
            if self._cipher is None:
                raise EncryptionKeyRequired("Set TELEGRAM_SESSION_ENCRYPTION_KEY to store secrets.")
        return normalized

    def _serialized(self, key: str, normalized: Any) -> tuple[str, bool]:
        typ = SETTINGS[key][0]
        if typ == "secret":
            assert self._cipher is not None
            return self._cipher.encrypt(str(normalized)).decode("utf-8"), True
        if typ == "bool":
            return ("true" if bool(normalized) else "false"), False
        return str(normalized), False

    async def set_many(self, changes: dict[str, Any], *, reset_keys: tuple[str, ...] = ()) -> None:
        """Validate and apply one form submission as a single PostgreSQL transaction."""
        if not self.available:
            raise StoreUnavailable("Settings store is not available in SQLite mode")

        resets = tuple(dict.fromkeys(reset_keys))
        for key in resets:
            if key not in SETTINGS:
                raise ValueError(f"unknown settings key: {key}")
            if key in changes:
                raise ValueError(f"setting cannot be saved and reset together: {key}")

        prepared: dict[str, tuple[Any, str, bool]] = {}
        for key, value in changes.items():
            normalized = self.validate(key, value)
            stored, is_secret = self._serialized(key, normalized)
            prepared[key] = (normalized, stored, is_secret)

        ws_id = getattr(self._db, "workspace_id", None)
        if ws_id is None:
            ws_id = self._db._workspace()

        if hasattr(self._db, "sessions"):
            from sqlalchemy import text as sql_text

            async with self._db.sessions.session() as session:
                for key in resets:
                    await session.execute(
                        sql_text("DELETE FROM workspace_settings WHERE workspace_id=:workspace_id AND key=:key"),
                        {"workspace_id": ws_id, "key": key},
                    )
                for key, (_normalized, stored, is_secret) in prepared.items():
                    await session.execute(
                        sql_text(
                            """INSERT INTO workspace_settings(workspace_id, key, value, is_secret, updated_at)
                               VALUES (:workspace_id, :key, :value, :is_secret, now())
                               ON CONFLICT (workspace_id, key) DO UPDATE SET
                                   value=EXCLUDED.value,
                                   is_secret=EXCLUDED.is_secret,
                                   updated_at=now()"""
                        ),
                        {"workspace_id": ws_id, "key": key, "value": stored, "is_secret": is_secret},
                    )
                await session.commit()
        elif hasattr(self._db, "_execute"):
            for key in resets:
                await self._db._execute(
                    "DELETE FROM workspace_settings WHERE workspace_id=:workspace_id AND key=:key",
                    {"key": key},
                )
            for key, (_normalized, stored, is_secret) in prepared.items():
                await self._db._execute(
                    """INSERT INTO workspace_settings(workspace_id, key, value, is_secret, updated_at)
                       VALUES (:workspace_id, :key, :value, :is_secret, now())
                       ON CONFLICT (workspace_id, key) DO UPDATE SET
                           value=EXCLUDED.value,
                           is_secret=EXCLUDED.is_secret,
                           updated_at=now()""",
                    {"key": key, "value": stored, "is_secret": is_secret},
                )
        else:
            fake = getattr(self._db, "_fake_settings", {})
            for key in resets:
                fake.pop(key, None)
            for key, (_normalized, stored, is_secret) in prepared.items():
                fake[key] = {"value": stored, "is_secret": is_secret}
            self._db._fake_settings = fake

        stamp = datetime.now(timezone.utc)
        for key in resets:
            self._rows.pop(key, None)
        for key, (normalized, stored, is_secret) in prepared.items():
            self._rows[key] = {
                "value": normalized,
                "is_secret": is_secret,
                "updated_at": stamp,
                "raw": stored,
            }

    async def set(self, key: str, value: Any) -> None:
        await self.set_many({key: value})

    async def reset(self, key: str) -> None:
        await self.set_many({}, reset_keys=(key,))

    def as_dict(self) -> dict[str, dict[str, Any]]:
        """Return dict of key -> {value, source, set} etc."""
        result: dict[str, dict[str, Any]] = {}
        for key, (typ, env_attr, _validator) in SETTINGS.items():
            if key in self._rows:
                row = self._rows[key]
                stamp = format_timestamp(row.get("updated_at"))
                if typ == "secret":
                    result[key] = {"set": bool(row["value"]), "source": "db", "updated_at": stamp}
                else:
                    result[key] = {"value": row["value"], "source": "db", "updated_at": stamp}
            else:
                env_val = getattr(self._base, env_attr, None)
                model_fields = type(self._base).model_fields
                field_default = model_fields[env_attr].default if env_attr in model_fields else None
                is_explicit = env_attr in getattr(self._base, "model_fields_set", set())
                if typ == "secret":
                    is_set = bool(env_val)
                    # Consider explicit set via env if is_explicit and non-empty, or if env_val truthy
                    if is_explicit and is_set:
                        source = "env"
                    elif is_set:
                        source = "env"
                    else:
                        source = "default"
                    result[key] = {"set": is_set, "source": source}
                else:
                    if is_explicit:
                        result[key] = {"value": env_val, "source": "env"}
                    else:
                        # Not explicitly set, check if env_val differs from default due to env file? Still treat as default if not explicit
                        # But if env file set it, it would be in model_fields_set
                        if env_val != field_default:
                            result[key] = {"value": env_val, "source": "env"}
                        else:
                            result[key] = {"value": env_val, "source": "default"}
        return result

    @property
    def effective(self) -> RuntimeSettings:
        """Return proxy that overlays DB values onto base Settings."""
        return RuntimeSettings(self._base, workspace_settings=self)

    def __repr__(self) -> str:
        # Never include secret plaintext
        safe = {k: ({"set": bool(v["value"])} if SETTINGS[k][0] == "secret" else v["value"]) for k, v in self._rows.items()}
        return f"<WorkspaceSettings available={self.available} rows={safe!r}>"

    __str__ = __repr__
