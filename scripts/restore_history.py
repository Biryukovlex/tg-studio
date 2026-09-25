"""Diagnose or run a complete Telegram post/comment history refresh.

PostgreSQL is the only runtime.  The command never modifies the SQLite
archive: diagnostic mode inspects it read-only through
``app.migration.sqlite_inventory`` and run mode refreshes the PostgreSQL
archive from Telegram with ``TRACK_DAYS=0`` and an unbounded iterator.
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

from app.collector import Collector
from app.config import Settings, load_settings
from app.migration.sqlite_inventory import _read_only_connection
from app.postgres_db import PostgresDatabase
from app.session_crypto import build_cipher


def _diagnostic(settings: Settings) -> dict:
    if settings.postgres_enabled:
        return {"message": "use --run for PostgreSQL diagnostics after startup", "storage": "postgresql"}
    archive = Path(settings.data_dir) / "stats.db"
    with _read_only_connection(archive) as connection:
        posts = connection.execute("SELECT COUNT(*) AS n FROM posts").fetchone()["n"]
        oldest = connection.execute("SELECT MIN(posted_at) AS v FROM posts").fetchone()["v"]
        newest = connection.execute("SELECT MAX(posted_at) AS v FROM posts").fetchone()["v"]
        channels = connection.execute("SELECT COUNT(*) AS n FROM channels").fetchone()["n"]
    return {
        "storage": "sqlite",
        "read_only": True,
        "total_posts": int(posts or 0),
        "oldest_post": oldest,
        "newest_post": newest,
        "channels": int(channels or 0),
    }


async def _run(settings: Settings) -> dict:
    runtime_settings = settings.model_copy(update={"track_days": 0, "backfill_limit": 0})
    if not runtime_settings.postgres_enabled:
        raise RuntimeError("DATABASE_URL is required (PostgreSQL); the SQLite runtime was removed")
    db = PostgresDatabase.from_settings(runtime_settings)
    await db.init_db(admin_username=runtime_settings.admin_username)
    cipher = build_cipher(runtime_settings.telegram_session_encryption_key)
    session_string = await db.load_telegram_session(
        label=runtime_settings.telegram_connection_label, cipher=cipher
    ) or runtime_settings.session_string
    if not session_string:
        raise RuntimeError("no Telegram session available; set SESSION_STRING or persist an encrypted connection")
    client = TelegramClient(StringSession(session_string), runtime_settings.api_id, runtime_settings.api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized")
        collector = Collector(client, db, runtime_settings)
        failed = await collector.sync_channels()
        summary = await collector.poll_all(reason="history-restoration")
        summary["failed_channels"] = failed
        summary["history_mode"] = "all"
        return summary
    finally:
        await client.disconnect()
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="perform the Telegram refresh")
    args = parser.parse_args()
    settings = load_settings()
    try:
        result = asyncio.run(_run(settings)) if args.run else _diagnostic(settings)
    except Exception as exc:  # noqa: BLE001 - concise CLI failure
        print(json.dumps({"status": "error", "error": type(exc).__name__, "detail": str(exc)[:300]}), file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
