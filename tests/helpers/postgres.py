"""Disposable PostgreSQL URL resolution for integration tests.

CI provides a single ``TEST_POSTGRES_URL``. Historical per-milestone names
(``M0_POSTGRES_URL`` … ``M7_POSTGRES_URL``) remain supported as fallbacks for
one release; pass them explicitly so the migration path stays visible.
"""

from __future__ import annotations

import os


def pg_url(*fallbacks: str) -> str:
    """Return the first configured PostgreSQL URL, or an empty string."""
    for key in ("TEST_POSTGRES_URL", *fallbacks):
        value = os.environ.get(key)
        if value:
            return value
    return ""
