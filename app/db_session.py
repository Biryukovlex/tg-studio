"""Async SQLAlchemy engine/session helpers for the PostgreSQL runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def normalize_database_url(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("postgresql://"):
        return "postgresql+asyncpg://" + value.removeprefix("postgresql://")
    if value.startswith("postgres://"):
        return "postgresql+asyncpg://" + value.removeprefix("postgres://")
    return value


class DatabaseSessionManager:
    def __init__(
        self,
        database_url: str,
        *,
        pool_size: int = 5,
        max_overflow: int = 5,
        pool_timeout: float = 30.0,
        pool_recycle: int = 1800,
    ) -> None:
        url = normalize_database_url(database_url)
        if not url.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL")
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_size=max(1, pool_size),
            max_overflow=max(0, max_overflow),
            pool_timeout=max(1.0, pool_timeout),
            pool_recycle=max(60, pool_recycle),
            pool_pre_ping=True,
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def healthcheck(self) -> None:
        from sqlalchemy import text

        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        await self.engine.dispose()
