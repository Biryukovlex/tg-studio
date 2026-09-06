"""Compatibility mapping for AG-UI's opaque external run identifiers."""

from __future__ import annotations

import re
import uuid


_SAFE_EXTERNAL_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_EXTERNAL_RUN_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "https://github.com/Biryukovlex/tg-studio/ag-ui-run-id/v1",
)


def resolve_run_id(value: object, *, workspace_id: uuid.UUID) -> uuid.UUID:
    """Return a database UUID for either UUID or safe opaque AG-UI IDs.

    AG-UI defines ``runId`` as a string. Current assistant-ui releases use a
    short Nano ID, while Studio stores run primary keys as PostgreSQL UUIDs.
    Preserve UUID inputs and map other safe identifiers deterministically so
    create, event polling, details, and cancellation resolve the same row.
    """

    raw = str(value).strip()
    if not raw:
        raise ValueError("run identifier is empty")
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        if _SAFE_EXTERNAL_RUN_ID.fullmatch(raw) is None:
            raise ValueError("run identifier contains unsupported characters")
        return uuid.uuid5(_EXTERNAL_RUN_NAMESPACE, f"{workspace_id}:{raw}")
