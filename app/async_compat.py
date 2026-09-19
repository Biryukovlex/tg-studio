"""Small boundary helper while the legacy SQLite repository is phased out."""

from __future__ import annotations

import inspect
from typing import Any


async def maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value
