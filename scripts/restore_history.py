"""Diagnose or run a complete Telegram post/comment history refresh.

The command never modifies the SQLite source in diagnostic mode.  In run mode
it uses the configured runtime repository and forces ``TRACK_DAYS=0`` and an
unbounded message iterator; the existing archive remains untouched when the
PostgreSQL importer was used as the cutover source.
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
from app.db import Database
from app.postgres_db import PostgresDatabase
from app.session_crypto import build_cipher


def _diagnostic(settings: Settings) -> dict:
    if settings.postgres_enabled:
        return {"message": "use --run for PostgreSQL diagnostics after startup", "storage": "postgresql"}
    db = Database(settings.db_path)
    db.init_db()
    return {"storage": "sqlite", **db.history_diagnostic()}


async def _run(settings: Settings) -> dict:
    runtime_settings = settings.model_copy(update={"track_days": 0, "backfill_limit": 0})
    if runtime_settings.postgres_enabled:
        db = PostgresDatabase.from_settings(runtime_settings)
        await db.init_db(admin_username=runtime_settings.admin_username)
        cipher = build_cipher(runtime_settings.telegram_session_encryption_key)
        session_string = await db.load_telegram_session(
            label=runtime_settings.telegram_connection_label, cipher=cipher
        ) or runtime_settings.session_string
    else:
        db = Database(runtime_settings.db_path)
        db.init_db()
        session_string = runtime_settings.session_string
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
        if isinstance(db, PostgresDatabase):
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
