"""Admin commands over the same user session.

Send these in your **Saved Messages** (chat with yourself):

    /help            - list commands
    /stats [channel] - totals for tracked posts (optionally one channel)
    /top [n]         - top n posts by views (default 5)
    /last [n]        - n most recent posts (default 5)
    /refresh         - force a collection cycle right now
"""

from __future__ import annotations

import logging

from telethon import TelegramClient, events

from datetime import datetime

from .collector import Collector
from .config import Settings
from .async_compat import maybe_await

log = logging.getLogger("commands")

HELP = (
    "TG Studio\n\n"
    "/stats [channel] - totals for tracked posts\n"
    "/top [n] - top posts by views\n"
    "/last [n] - most recent posts\n"
    "/refresh - collect stats now\n"
)


def _post_link(row) -> str:
    identifier = (row["identifier"] or "").lstrip("@")
    chat_id = row["chat_id"] or 0
    if identifier and not identifier.isdigit() and not identifier.startswith("http"):
        return f"https://t.me/{identifier}/{row['message_id']}"
    if chat_id:
        return f"https://t.me/c/{chat_id}/{row['message_id']}"
    return "(no link)"


def _fmt_row(row, with_channel: bool = False) -> str:
    text = (row["text"] or "").strip().replace("\n", " ")[:60] or "(media)"
    ch = f" [{row['identifier']}]" if with_channel else ""
    return (
        f"- {_post_link(row)}{ch} - {row['views']:,} views | "
        f"{row['reactions']:,} reactions | {row['comments']:,} comments | "
        f"{row['shares']:,} shares\n  {text}"
    )


class CommandHandlers:
    def __init__(self, client: TelegramClient, collector: Collector, settings: Settings) -> None:
        self.client = client
        self.collector = collector
        self.db = collector.db
        self.settings = settings

    async def start(self) -> None:
        me = await self.client.get_me()
        self._me_id = me.id

        @self.client.on(events.NewMessage(pattern=r"^/(\w+)"))
        async def on_command(event) -> None:
            try:
                await self._dispatch(event)
            except Exception:  # noqa: BLE001
                log.exception("command failed")
                await event.reply("Something went wrong, check logs.")

    def _allowed(self, event) -> bool:
        if not event.is_private:
            return False
        # Saved Messages: the account owner talking to themselves.
        return bool(event.out and event.chat_id == self._me_id)

    async def _find_channel_id(self, arg: str):
        ident = arg.strip().lstrip("@").lower()
        for ch in await maybe_await(self.db.get_channels()):
            if ch["identifier"].lstrip("@").lower() == ident:
                return ch["id"]
        return None

    async def _dispatch(self, event) -> None:
        parts = event.raw_text.split(maxsplit=1)
        cmd = parts[0].lstrip("/").lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if not self._allowed(event):
            return
        if not event.is_private:
            return

        if cmd in ("start", "help"):
            await event.reply(HELP)
            return

        if cmd == "refresh":
            await event.reply("Collecting fresh stats...")
            # Use supervised schedule_poll to avoid fire-and-forget without logging
            scheduler = getattr(self.collector, "schedule_poll", None)
            if scheduler is not None:
                task = scheduler(reason="manual")
                try:
                    summary = await task
                except Exception:
                    await event.reply("Collection failed; check logs.")
                    return
            else:
                summary = await self.collector.poll_all(reason="manual")
            await event.reply(
                f"Done: {summary['posts_seen']} posts scanned, "
                f"{summary['snapshots_written']} snapshots updated, "
                f"{summary['comments_seen']} comments archived "
                f"({summary['comments_written']} changed)."
            )
            return

        channel_id = None
        if cmd == "stats" and arg:
            channel_id = await self._find_channel_id(arg)
            if channel_id is None:
                known_channels = await maybe_await(self.db.get_channels())
                known = ", ".join(c["identifier"] for c in known_channels) or "(none)"
                await event.reply(f"Unknown channel. Tracked: {known}")
                return

        if cmd == "stats":
            k = await maybe_await(self.db.kpis(channel_id))
            scope = arg if arg else "all channels"
            raw_last = k.get("last_poll")
            if isinstance(raw_last, datetime):
                last_poll = raw_last.strftime("%Y-%m-%d %H:%M") + " UTC"
            else:
                last_poll = (raw_last or "never") + " UTC"
            await event.reply(
                f"Stats for {scope}\n\n"
                f"Posts tracked: {k.get('posts', 0):,}\n"
                f"Views: {k.get('views', 0):,}\n"
                f"Reactions: {k.get('reactions', 0):,}\n"
                f"Comments: {k.get('comments', 0):,}\n"
                f"Shares: {k.get('shares', 0):,}\n\n"
                f"Last update: {last_poll}"
            )
        elif cmd == "top":
            n = int(arg.split()[0]) if arg and arg.split()[0].isdigit() else 5
            rows = await maybe_await(self.db.latest_stats(limit=n, order="views"))
            body = "\n".join(_fmt_row(r, with_channel=True) for r in rows) or "nothing tracked yet"
            await event.reply(f"Top {len(rows)} by views\n\n{body}")
        elif cmd == "last":
            n = int(arg.split()[0]) if arg and arg.split()[0].isdigit() else 5
            rows = await maybe_await(self.db.latest_stats(limit=n, order="date"))
            body = "\n".join(_fmt_row(r, with_channel=True) for r in rows) or "nothing tracked yet"
            await event.reply(f"Last {len(rows)} posts\n\n{body}")
