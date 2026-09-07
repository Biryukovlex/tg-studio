"""Stats collection via a Telethon *user* session.

One pass over each channel's configured history yields post metrics plus the
actual comments from linked discussion threads:
    views     -> message.views
    comments  -> message.replies.replies   (requires a linked discussion group)
    reactions -> sum of message.reactions.results[].count
    shares    -> message.forwards          (forward counter shown on the post)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from telethon import TelegramClient, errors

from . import limits
from .config import Settings
from .db import Database, utcnow
from .async_compat import maybe_await
from .telegram_formatting import serialize_entities

log = logging.getLogger("collector")


def extract_metrics(msg: Any) -> tuple[int, int, int, int]:
    """Return (views, comments, reactions, shares) for a channel message."""
    views = int(getattr(msg, "views", 0) or 0)
    forwards = int(getattr(msg, "forwards", 0) or 0)

    replies_obj = getattr(msg, "replies", None)
    comments = int(getattr(replies_obj, "replies", 0) or 0) if replies_obj else 0

    reactions = 0
    rx = getattr(msg, "reactions", None)
    for rc in (getattr(rx, "results", None) or []):
        reactions += int(getattr(rc, "count", 0) or 0)

    return views, comments, reactions, forwards


def _sender_details(msg: Any) -> tuple[int | None, str, str]:
    """Return a stable local attribution for a comment author when available."""
    sender = getattr(msg, "sender", None)
    sender_id = getattr(msg, "sender_id", None)
    if sender_id is not None:
        sender_id = int(sender_id)

    username = str(getattr(sender, "username", "") or "") if sender else ""
    if sender is not None:
        title = str(getattr(sender, "title", "") or "").strip()
        first = str(getattr(sender, "first_name", "") or "").strip()
        last = str(getattr(sender, "last_name", "") or "").strip()
        name = title or " ".join(p for p in (first, last) if p) or username
    else:
        name = str(getattr(msg, "post_author", "") or "").strip()

    return sender_id, name or "Unknown / hidden", username


class Collector:
    def __init__(self, client: TelegramClient | None, db: Database, settings: Settings) -> None:
        self.client = client
        self.db = db
        self.settings = settings
        self.workspace_settings = None  # type: ignore[attr-defined]
        self.on_poll_interval_change = None  # type: ignore[attr-defined]
        self._entities: dict[str, Any] = {}
        self._lock = asyncio.Lock()  # one cycle at a time (scheduler + manual refresh)
        self._background: asyncio.Task | None = None
        self._next_allowed_at: float = 0.0
        self._last_full_scan: dict[int, datetime] = {}

    async def resolve_entity(self, identifier: str) -> Any:
        if identifier in self._entities:
            return self._entities[identifier]
        entity = await self.client.get_entity(identifier)
        self._entities[identifier] = entity
        await maybe_await(self.db.upsert_channel(
            identifier,
            title=getattr(entity, "title", "") or "",
            chat_id=int(getattr(entity, "id", 0) or 0) or None,
        ))
        return entity

    async def sync_channels(self) -> list[str]:
        """Resolve configured channels into the DB. Returns identifiers that failed."""
        failed: list[str] = []
        # Union of env channel_list and DB rows with chat_id IS NULL (added via page)
        idents = list(self.settings.channel_list)
        # Also include DB rows with chat_id IS NULL (not yet resolved)
        try:
            # Fetch all channels and filter those with chat_id is None and not already in idents
            channels = await maybe_await(self.db.get_channels())
            # get_channels only returns active=true, but we need to include those with chat_id NULL even if active?
            # For Postgres, we can query directly for chat_id IS NULL
            # Use _execute if available to get all with chat_id NULL
            if hasattr(self.db, "_execute"):
                try:
                    result = await self.db._execute(
                        "SELECT identifier FROM channels WHERE workspace_id=:workspace_id AND chat_id IS NULL AND active=true",
                        {},
                    )
                    for row in result.mappings().all():
                        ident = row["identifier"]
                        if ident not in idents:
                            idents.append(ident)
                except Exception:
                    pass
            else:
                for ch in channels:
                    if ch.get("chat_id") is None and ch.get("identifier") not in idents:
                        idents.append(ch["identifier"])
        except Exception:
            pass
        for ident in idents:
            try:
                await self.resolve_entity(ident)
                log.info("channel %s resolved", ident)
            except Exception as exc:  # noqa: BLE001 - report and continue
                log.error("cannot resolve channel %s: %s", ident, exc)
                failed.append(ident)
        return failed

    def schedule_poll(self, reason: str = "manual") -> asyncio.Task:
        """Supervised background poll that logs exceptions."""

        task = asyncio.create_task(self.poll_all(reason=reason))
        self._background = task

        def _done(t: asyncio.Task) -> None:
            try:
                t.result()
            except Exception as exc:  # noqa: BLE001
                log.exception("background poll (%s) failed: %s", reason, exc)
            finally:
                if self._background is t:
                    self._background = None

        task.add_done_callback(_done)
        return task

    async def sync_comments(self, entity: Any, post_id: int, message_id: int) -> tuple[int, int, int]:
        """Collect every current comment for a channel post's discussion thread."""
        sync_token = utcnow().isoformat(timespec="microseconds")
        seen = changed = 0

        async for comment in self.client.iter_messages(entity, reply_to=message_id, limit=None):
            if comment.action is not None or comment.date is None:
                continue
            seen += 1
            sender_id, sender_name, sender_username = _sender_details(comment)
            chat = getattr(comment, "chat", None)
            reply = getattr(comment, "reply_to", None)
            media = getattr(comment, "media", None)
            reactions = extract_metrics(comment)[2]

            if await maybe_await(self.db.upsert_comment(
                post_id=post_id,
                telegram_message_id=int(comment.id),
                discussion_chat_id=int(getattr(comment, "chat_id", 0) or 0),
                discussion_username=str(getattr(chat, "username", "") or ""),
                sender_id=sender_id,
                sender_name=sender_name,
                sender_username=sender_username,
                posted_at=comment.date,
                edited_at=getattr(comment, "edit_date", None),
                text=comment.message or "",
                media_type=type(media).__name__ if media is not None else "",
                reactions=reactions,
                reply_to_message_id=getattr(reply, "reply_to_msg_id", None),
                sync_token=sync_token,
            )):
                changed += 1

        deleted = await maybe_await(self.db.mark_unseen_comments_deleted(post_id, sync_token))
        return seen, changed, deleted

    async def poll_channel(self, ch: Any, job_id: Any = None) -> tuple[int, int, int, int, int, int]:
        """Scan one channel. Return post, snapshot, and comment collection counts."""

        entity = await self.resolve_entity(ch["identifier"])
        cutoff = None
        is_full_scan = False
        if self.settings.track_days > 0:
            cutoff = utcnow().replace(tzinfo=None) - timedelta(days=self.settings.track_days)
            limit = self.settings.backfill_limit if self.settings.backfill_limit > 0 else None
        else:
            # Whole-history mode with pacing: full scan only every FULL_RESCAN_HOURS
            full_hours = int(limits.FULL_RESCAN_HOURS)
            now = datetime.now(timezone.utc)
            last = self._last_full_scan.get(ch["id"])
            if last is None or (now - last).total_seconds() >= full_hours * 3600:
                cutoff = None
                limit = None
                is_full_scan = True
            else:
                recent_minutes = max(float(self.settings.poll_minutes) * 2, 60)
                cutoff = utcnow().replace(tzinfo=None) - timedelta(minutes=recent_minutes)
                limit = self.settings.backfill_limit if self.settings.backfill_limit > 0 else None

        # has_comments optimization: fetch post IDs with comments in one query
        commented_ids: set[int] = set()
        getter = getattr(self.db, "post_ids_with_comments", None)
        if getter is not None:
            try:
                commented_ids = set(await maybe_await(getter(ch["id"])))
            except Exception:
                commented_ids = set()
        else:
            # Fallback: try generic all_comments then filter, but avoid heavy load
            pass

        seen = written = comments_seen = comments_written = comments_deleted = comment_errors = 0
        last_renew = time.monotonic()
        async for msg in self.client.iter_messages(entity, limit=limit):
            if msg.action is not None:
                continue
            if msg.date is None:
                continue
            if cutoff is not None and msg.date.replace(tzinfo=None) < cutoff:
                break
            seen += 1
            # renew lease every 60s during long scans
            if job_id is not None and time.monotonic() - last_renew >= 60:
                renew = getattr(self.db, "renew_collection_job", None)
                if renew is not None:
                    try:
                        await maybe_await(renew(job_id))
                    except Exception:
                        pass
                last_renew = time.monotonic()
            post_id = await maybe_await(self.db.upsert_post(
                ch["id"],
                msg.id,
                msg.date,
                msg.message or "",
                formatting_entities=serialize_entities(getattr(msg, "entities", None)),
            ))
            views, comments, reactions, shares = extract_metrics(msg)
            if await maybe_await(self.db.add_snapshot_if_changed(post_id, views, comments, reactions, shares)):
                written += 1
            should_fetch = False
            if comments > 0:
                should_fetch = True
            elif commented_ids:
                should_fetch = post_id in commented_ids
            else:
                # fallback per-post check
                try:
                    should_fetch = bool(await maybe_await(self.db.has_comments(post_id)))
                except Exception:
                    should_fetch = False
            if should_fetch:
                try:
                    c_seen, c_written, c_deleted = await self.sync_comments(entity, post_id, msg.id)
                    comments_seen += c_seen
                    comments_written += c_written
                    comments_deleted += c_deleted
                    await asyncio.sleep(0.5)
                except errors.FloodWaitError:
                    raise
                except errors.RPCError as exc:
                    comment_errors += 1
                    log.warning("comment sync failed for post %s (%s): %s", post_id, msg.id, type(exc).__name__)
                    continue
                except Exception as exc:  # noqa: BLE001 - keep scanning on unexpected comment errors
                    comment_errors += 1
                    log.warning("comment sync failed for post %s (%s): %s", post_id, msg.id, type(exc).__name__)
                    continue
        if is_full_scan:
            self._last_full_scan[ch["id"]] = datetime.now(timezone.utc)
        return seen, written, comments_seen, comments_written, comments_deleted, comment_errors

    async def poll_all(self, reason: str = "scheduled") -> dict[str, Any]:
        # Cross-process refresh: reload workspace settings and reschedule if poll interval changed
        if getattr(self, "workspace_settings", None) is not None:
            try:
                prev = float(self.settings.poll_minutes)
                await self.workspace_settings.load()  # type: ignore[attr-defined]
                new = float(self.settings.poll_minutes)
                if new != prev and getattr(self, "on_poll_interval_change", None):
                    try:
                        self.on_poll_interval_change(new)  # type: ignore[attr-defined]
                    except Exception:
                        log.warning("on_poll_interval_change failed")
            except Exception:
                log.warning("workspace_settings load failed in poll_all")
        async with self._lock:
            started = utcnow()
            total_seen = total_new = 0
            total_comments = total_comments_changed = total_comments_deleted = total_comment_errors = 0
            err_count = 0
            channels = await maybe_await(self.db.get_channels())
            for ch in channels:
                if time.monotonic() < self._next_allowed_at:
                    log.info("skipping channel %s due to active FloodWait", ch["identifier"])
                    continue
                job_id = None
                channel_failed = False
                failure_code = None
                claim = getattr(self.db, "claim_collection_job", None)
                if claim is not None:
                    job_id = await maybe_await(claim(ch["id"]))
                    if job_id is None:
                        log.info("collection already running for channel %s; skipping overlap", ch["identifier"])
                        continue
                try:
                    seen, written, comments, comments_changed, comments_deleted, comment_errors = (
                        await self.poll_channel(ch, job_id=job_id)
                    )
                    total_seen += seen
                    total_new += written
                    total_comments += comments
                    total_comments_changed += comments_changed
                    total_comments_deleted += comments_deleted
                    total_comment_errors += comment_errors
                except errors.FloodWaitError as exc:
                    log.warning("flood wait %ss while polling %s", exc.seconds, ch["identifier"])
                    channel_failed = True
                    failure_code = type(exc).__name__
                    self._next_allowed_at = time.monotonic() + float(exc.seconds)
                    err_count += 1
                    await asyncio.sleep(min(float(exc.seconds), 30))
                except Exception as exc:  # noqa: BLE001
                    err_count += 1
                    channel_failed = True
                    failure_code = type(exc).__name__
                    log.exception("poll failed for %s", ch["identifier"])
                finally:
                    finish = getattr(self.db, "finish_collection_job", None)
                    if finish is not None and job_id is not None:
                        await maybe_await(
                            finish(
                                job_id,
                                status="failed" if channel_failed else "succeeded",
                                error=failure_code,
                            )
                        )
            summary = {
                "reason": reason,
                "started": started,
                "finished": utcnow(),
                "posts_seen": total_seen,
                "snapshots_written": total_new,
                "comments_seen": total_comments,
                "comments_written": total_comments_changed,
                "comments_deleted": total_comments_deleted,
                "comment_errors": total_comment_errors,
                "errors": err_count,
            }
            log.info(
                "cycle(%s): %s posts, %s snapshots, %s comments (%s changed, %s deleted) (%.1fs)",
                reason, total_seen, total_new, total_comments,
                total_comments_changed, total_comments_deleted,
                (summary["finished"] - started).total_seconds(),
            )
            return summary
