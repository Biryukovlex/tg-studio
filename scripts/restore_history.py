"""Diagnose or run a complete Telegram post/comment history refresh.

Diagnostic mode reads PostgreSQL without initializing or changing a workspace.
Explicit ``--run`` mode refreshes PostgreSQL from Telegram using the saved
workspace connection and settings, with an unbounded history window.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from telethon import TelegramClient
from telethon.sessions import StringSession
from sqlalchemy import text

from app import limits
from app.collector import Collector
from app.config import Settings, load_settings
from app.main import resolve_telegram_connection, telegram_connection_problems
from app.postgres_db import PostgresDatabase
from app.session_crypto import build_cipher
from app.workspace_settings import RuntimeSettings, WorkspaceSettings


class HistoryError(RuntimeError):
    """An operator-facing error whose text contains no connection secrets."""


def _database(settings: Settings) -> PostgresDatabase:
    if not settings.postgres_enabled:
        raise HistoryError("DATABASE_URL is required (PostgreSQL)")
    return PostgresDatabase.from_settings(settings)


async def _diagnostic(settings: Settings) -> dict:
    db = _database(settings)
    try:
        async with db.sessions.session() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            result = await session.execute(text("""
                SELECT COUNT(p.id) AS total_posts,
                       MIN(p.posted_at) AS oldest_post,
                       MAX(p.posted_at) AS newest_post,
                       COUNT(DISTINCT c.id) AS channels
                FROM workspaces w
                LEFT JOIN channels c ON c.workspace_id = w.id
                LEFT JOIN posts p ON p.workspace_id = w.id AND p.channel_id = c.id
                WHERE w.slug = :slug
                GROUP BY w.id
            """), {"slug": db.workspace_slug})
            row = result.mappings().first()
            if row is None:
                raise HistoryError("Workspace is not initialized; start the application first")
            return {"storage": "postgresql", "read_only": True, **dict(row)}
    finally:
        await db.close()


async def _run(settings: Settings) -> dict:
    db = _database(settings)
    client = None
    try:
        await db.init_db(admin_username=settings.admin_username)
        cipher = build_cipher(settings.telegram_session_encryption_key)
        workspace_settings = WorkspaceSettings(db, settings, cipher)
        await workspace_settings.load()
        runtime_settings = RuntimeSettings(
            workspace_settings.effective, overlay={"track_days": 0, "backfill_limit": 0}
        )
        persisted = await db.load_telegram_connection(label=limits.TELEGRAM_CONNECTION_LABEL, cipher=cipher)
        connection = resolve_telegram_connection(settings, persisted)
        problems = telegram_connection_problems(connection)
        if problems:
            raise HistoryError(" ".join(problems))
        client = TelegramClient(
            StringSession(str(connection["session_string"])), int(connection["api_id"]), str(connection["api_hash"])
        )
        await client.connect()
        if not await client.is_user_authorized():
            raise HistoryError("Telegram session is not authorized")
        collector = Collector(client, db, runtime_settings)
        failed = await collector.sync_channels()
        summary = await collector.poll_all(reason="history-restoration")
        summary["failed_channels"] = failed
        summary["history_mode"] = "all"
        return summary
    finally:
        try:
            if client is not None:
                await client.disconnect()
        finally:
            await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="perform the Telegram refresh")
    args = parser.parse_args()
    settings = load_settings()
    try:
        result = asyncio.run(_run(settings) if args.run else _diagnostic(settings))
    except Exception as exc:  # noqa: BLE001 - concise CLI failure
        detail = str(exc) if isinstance(exc, HistoryError) else "History operation failed; check configuration and PostgreSQL availability"
        print(json.dumps({"status": "error", "error": type(exc).__name__, "detail": detail}), file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
