"""Entrypoint.

In the default ``all`` role this runs three things in one asyncio loop:
  1. Telethon user client  - stats collection + admin commands (/stats ...)
  2. APScheduler           - periodic polling every POLL_MINUTES
  3. Uvicorn/FastAPI       - web admin panel on WEB_HOST:WEB_PORT

Hosted deployments can set ``PROCESS_ROLE=web`` or ``PROCESS_ROLE=worker`` to
split those responsibilities without maintaining a second code path.

Ctrl+C stops everything gracefully (works the same under Docker/systemd).
"""

from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telethon import TelegramClient
from telethon.sessions import StringSession

from . import limits
from .bot import CommandHandlers
from .collector import Collector
from .config import load_settings
from .db import Database
from .postgres_db import PostgresDatabase
from .session_crypto import build_cipher
from .web.routes import create_app
from .workspace_settings import WorkspaceSettings


async def amain() -> None:
    settings = load_settings()
    role = str(settings.process_role or "all").strip().lower()

    problems = settings.validate_required()
    if problems:
        print("\nConfiguration problems found:\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}\n", file=sys.stderr)
        print("Fix .env (copy .env.example) and run again.", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("main")
    log.info("process role: %s", role)

    if settings.postgres_enabled:
        postgres_problems = settings.validate_postgres()
        if postgres_problems:
            print("\nPostgreSQL configuration problems found:\n", file=sys.stderr)
            for problem in postgres_problems:
                print(f"  - {problem}", file=sys.stderr)
            sys.exit(1)
        db = PostgresDatabase.from_settings(settings)
        await db.init_db(admin_username=settings.admin_username)
        # After DB init, re-validate CHANNELS with DB state
        if not settings.channel_list:
            try:
                channels = await db.get_channels()
                if not channels:
                    print("\nConfiguration problems found:\n", file=sys.stderr)
                    print("  - CHANNELS empty - e.g. CHANNELS=@my_channel\n", file=sys.stderr)
                    print("Fix .env (copy .env.example) and run again.", file=sys.stderr)
                    sys.exit(1)
            except Exception:
                pass
    else:
        db = Database(settings.db_path)
        db.init_db()

    def _uvicorn_config(app) -> uvicorn.Config:
        kwargs: dict = dict(host=settings.web_host, port=settings.web_port, log_level="info")
        if settings.trusted_proxy_ips.strip():
            kwargs["proxy_headers"] = True
            kwargs["forwarded_allow_ips"] = settings.trusted_proxy_ips.strip()
        return uvicorn.Config(app, **kwargs)

    # Build workspace settings accessor
    cipher = build_cipher(settings.telegram_session_encryption_key)
    if isinstance(db, PostgresDatabase):
        workspace_settings = WorkspaceSettings(db, settings, cipher)
        await workspace_settings.load()
        effective = workspace_settings.effective
    else:
        # SQLite mode: no-op accessor
        workspace_settings = WorkspaceSettings(db, settings, cipher)
        # Force available=False for SQLite
        workspace_settings.available = False
        workspace_settings._rows = {}
        workspace_settings._loaded = True
        effective = workspace_settings.effective

    # The web role serves read-only dashboard/Studio requests and deliberately
    # does not acquire a Telegram session. Collection/manual refresh is owned
    # by the worker role in split deployments.
    if role == "web":
        collector = Collector(None, db, effective)
        collector.workspace_settings = workspace_settings  # type: ignore[attr-defined]
        app = create_app(collector, effective, workspace_settings=workspace_settings)
        server = uvicorn.Server(_uvicorn_config(app))
        log.info("Web-only panel: http://%s:%s", settings.web_host, settings.web_port)
        try:
            await server.serve()
        finally:
            if isinstance(db, PostgresDatabase):
                await db.close()
        return

    persisted_session = None
    if isinstance(db, PostgresDatabase):
        persisted_session = await db.load_telegram_session(
            label=limits.TELEGRAM_CONNECTION_LABEL, cipher=cipher
        )
        try:
            await db.expire_stale_collection_jobs()
        except Exception:
            log.warning("expire_stale_collection_jobs failed at startup")
    session_string = persisted_session or settings.session_string
    if not session_string:
        raise RuntimeError(
            "No Telegram session is available. Provide SESSION_STRING for the first login "
            "or import an encrypted connection row into PostgreSQL."
        )

    client = TelegramClient(
        StringSession(session_string), settings.api_id, settings.api_hash
    )
    await client.connect()
    if not await client.is_user_authorized():
        # Try environment session if it differs from persisted
        if settings.session_string and settings.session_string != persisted_session:
            log.info("Persisted session not authorized, trying environment session")
            try:
                await client.disconnect()
            except Exception:
                pass
            client = TelegramClient(
                StringSession(settings.session_string), settings.api_id, settings.api_hash
            )
            await client.connect()
            if await client.is_user_authorized():
                session_string = settings.session_string
                log.info("session source: environment")
                if isinstance(db, PostgresDatabase) and cipher is not None:
                    await db.persist_telegram_session(
                        label=limits.TELEGRAM_CONNECTION_LABEL,
                        api_id=settings.api_id,
                        api_hash=settings.api_hash,
                        session_string=session_string,
                        cipher=cipher,
                    )
            else:
                log.error("Session is not authorized - re-run `python scripts/generate_session.py`.")
                return
        else:
            log.error("Session is not authorized - re-run `python scripts/generate_session.py`.")
            return
    else:
        if isinstance(db, PostgresDatabase) and cipher is not None and session_string != persisted_session:
            try:
                await db.persist_telegram_session(
                    label=limits.TELEGRAM_CONNECTION_LABEL,
                    api_id=settings.api_id,
                    api_hash=settings.api_hash,
                    session_string=session_string,
                    cipher=cipher,
                )
            except Exception:
                log.warning("persist_telegram_session failed")
    log.info("Logged in as %s", (await client.get_me()).first_name)

    collector = Collector(client, db, effective)
    collector.workspace_settings = workspace_settings  # type: ignore[attr-defined]
    failed = await collector.sync_channels()
    if failed:
        log.warning("Could not resolve channel(s): %s - check CHANNELS in .env", ", ".join(failed))

    handlers = CommandHandlers(client, collector, effective)
    await handlers.start()
    log.info("Telegram commands enabled for Saved Messages")

    scheduler = AsyncIOScheduler(timezone="UTC")

    def _on_poll_interval_change(new_minutes: float) -> None:
        try:
            scheduler.reschedule_job("poll_stats", trigger="interval", minutes=new_minutes)
            log.info("Rescheduled poll_stats to %s minutes", new_minutes)
        except Exception:
            log.warning("Failed to reschedule poll_stats")

    collector.on_poll_interval_change = _on_poll_interval_change  # type: ignore[attr-defined]

    scheduler.add_job(
        collector.poll_all,
        trigger="interval",
        minutes=effective.poll_minutes,
        kwargs={"reason": "scheduled"},
        id="poll_stats",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()

    async def _first_cycle() -> None:
        try:
            await collector.poll_all(reason="startup")
        except Exception:  # noqa: BLE001 - never let it kill the process
            log.exception("startup collection cycle failed")

    if role == "worker":
        log.info("Collector worker started; web serving is disabled for PROCESS_ROLE=worker")
        first_cycle = asyncio.create_task(_first_cycle())
        try:
            await client.run_until_disconnected()
        finally:
            if not first_cycle.done():
                first_cycle.cancel()
            await asyncio.gather(first_cycle, return_exceptions=True)
            scheduler.shutdown(wait=False)
            await client.disconnect()
            if isinstance(db, PostgresDatabase):
                await db.close()
        return

    app = create_app(collector, effective, workspace_settings=workspace_settings)
    server = uvicorn.Server(_uvicorn_config(app))

    log.info("Web panel: http://%s:%s", settings.web_host, settings.web_port)
    log.info("First collection cycle starting...")

    server_task = asyncio.create_task(server.serve())
    tg_task = asyncio.create_task(client.run_until_disconnected())

    first_cycle = asyncio.create_task(_first_cycle())

    try:
        # Wait ONLY on the long-running tasks. Including first_cycle here would
        # tear everything down as soon as the initial poll finishes.
        await asyncio.wait({server_task, tg_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (server_task, tg_task, first_cycle):
            if not task.done():
                task.cancel()
        await asyncio.gather(server_task, tg_task, first_cycle, return_exceptions=True)
        scheduler.shutdown(wait=False)
        try:
            await client.disconnect()
        except Exception:
            pass
        if isinstance(db, PostgresDatabase):
            try:
                await db.close()
            except Exception:
                pass


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
