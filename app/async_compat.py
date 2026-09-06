"""Small boundary helper while the legacy SQLite repository is phased out."""

from __future__ import annotations

import inspect
from typing import Awaitable, TypeVar


T = TypeVar("T")


async def maybe_await(value: T | Awaitable[T]) -> T:
    return await value if inspect.isawaitable(value) else value
