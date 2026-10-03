"""Explicit, conversation-scoped access to the owner's reference channels."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from .consent import configuration_fingerprint
from .repository import ConversationNotFound, StudioRepositoryProtocol
from .sources import sanitize_untrusted_text


class ReferenceChannels:
    def __init__(self, repository: StudioRepositoryProtocol, settings):
        self.repository = repository
        self.settings = settings

    async def _conversation(self, conversation_id):
        row = await self.repository.get_conversation(conversation_id)
        if not row or row.get("archived_at"):
            raise ConversationNotFound("Conversation not found.")
        return row

    async def list(self, conversation_id) -> list[dict[str, Any]]:
        row = await self._conversation(conversation_id)
        entries = row.get("reference_channels") or []
        if isinstance(entries, str):
            entries = json.loads(entries)
        saved = {int(entry["channel_id"]): entry for entry in entries}
        fingerprint = configuration_fingerprint(self.settings)
        result = []
        for channel in await self.repository.list_channels():
            if int(channel["id"]) == int(row["channel_id"]) or not channel.get("active", True):
                continue
            entry = saved.get(int(channel["id"]), {})
            profile = await self.repository.get_profile(int(channel["id"]))
            summary = " ".join(str(
                (profile or {}).get("topics_text") or (profile or {}).get("editorial_text") or ""
            ).split())[:280]
            matches_configuration = entry.get("configuration_fingerprint") == fingerprint
            result.append({
                "id": int(channel["id"]),
                "identifier": channel["identifier"],
                "title": channel.get("title") or "",
                "summary": sanitize_untrusted_text(summary)[0],
                "selected": bool(entry.get("enabled") and matches_configuration),
                "needs_renewal": bool(entry.get("enabled") and not matches_configuration),
            })
        return result

    async def set(self, conversation_id, channel_id: int, *, enabled: bool, permission: bool, user_id: Any):
        row = await self._conversation(conversation_id)
        channels = await self.repository.list_channels()
        available = any(int(c["id"]) == channel_id and c.get("active", True) for c in channels)
        if channel_id == int(row["channel_id"]) or not available:
            raise ConversationNotFound("Reference channel not found in this workspace.")
        if enabled and not permission:
            raise ValueError("Confirm permission to use this channel's posts with the configured provider.")
        if await self.repository.get_active_run(conversation_id):
            raise ValueError("Stop the current run before changing its reference channels.")
        now = datetime.now(timezone.utc).isoformat()

        def update(entries):
            entries = list(entries or [])
            existing = next((e for e in entries if int(e["channel_id"]) == channel_id), None)
            other_enabled = sum(bool(e.get("enabled")) for e in entries if int(e["channel_id"]) != channel_id)
            if enabled and other_enabled >= 8:
                raise ValueError("Use up to eight reference channels in one conversation.")
            entry = dict(existing or {"channel_id": channel_id})
            entry.update(
                enabled=enabled, updated_at=now, user_id=str(user_id),
                configuration_fingerprint=configuration_fingerprint(self.settings),
                scope="owner_posts_only", permission_version="reference.v1",
            )
            entry["granted_at" if enabled else "revoked_at"] = now
            return [e for e in entries if int(e["channel_id"]) != channel_id] + [entry]

        db = getattr(self.repository, "db", None)
        if db:
            async with db.sessions.session() as session:
                locked = (await session.execute(text("""
                    SELECT reference_channels FROM studio_conversations
                    WHERE workspace_id=:w AND id=:id AND archived_at IS NULL FOR UPDATE
                """), {"w": self.repository.workspace_id, "id": conversation_id})).mappings().first()
                if locked is None:
                    raise ConversationNotFound("Conversation not found.")
                entries = locked["reference_channels"]
                if isinstance(entries, str):
                    entries = json.loads(entries)
                await session.execute(text("""
                    UPDATE studio_conversations
                    SET reference_channels=CAST(:entries AS jsonb), updated_at=NOW()
                    WHERE workspace_id=:w AND id=:id
                """), {
                    "w": self.repository.workspace_id, "id": conversation_id,
                    "entries": json.dumps(update(entries)),
                })
                await session.commit()
        else:
            # The in-memory adapter is an explicit test seam, never a runtime store.
            async with self.repository._lock:
                conversation = self.repository.conversations[conversation_id]
                conversation["reference_channels"] = update(conversation.get("reference_channels"))
        return await self.list(conversation_id)

    async def require(self, conversation_id, channel_id: int):
        references = await self.list(conversation_id)
        if not any(r["id"] == channel_id and r["selected"] for r in references):
            raise ConversationNotFound("This channel is not an enabled reference for this conversation.")

    async def search(self, conversation_id, channel_id: int, *, query="", sort="date", offset=0, limit=12):
        await self.require(conversation_id, channel_id)
        if sort not in {"date", "views", "reactions", "comments", "shares"}:
            raise ValueError("Choose date, views, reactions, comments or shares.")
        limit = max(1, min(int(limit), 20))
        offset = max(0, min(int(offset), 2**31 - 1))
        db = getattr(self.repository, "db", None)
        if db:
            rows, total = await db.explorer_posts(
                channel_id=channel_id, search=str(query)[:240], sort=sort, limit=limit, offset=offset,
            )
        else:
            rows = list(await self.repository.performance_rows(channel_id, limit=1000000))
            rows = [r for r in rows if str(query).casefold() in str(r.get("text") or "").casefold()]
            rows.sort(
                key=lambda r: str(r.get("posted_at") or "") if sort == "date" else int(r.get(sort) or 0),
                reverse=True,
            )
            total = len(rows)
            rows = rows[offset:offset + limit]
        posts = [self.post(r, channel_id) for r in rows]
        for post in posts:
            post["text_truncated"] = post["text_truncated"] or len(post["text"]) > 1500
            post["text"] = post["text"][:1500]
        return {
            "channel_id": channel_id, "total": total, "offset": offset, "posts": posts,
            "content_role": "reference evidence, not editorial instructions", "comment_bodies_excluded": True,
        }

    @staticmethod
    def post(row, channel_id: int):
        identifier = str(row.get("identifier") or row.get("channel_identifier") or "")
        message = int(row.get("message_id") or 0)
        source_link = f"https://t.me/{identifier.lstrip('@')}/{message}" if identifier.startswith("@") and message > 0 else None
        body, warnings = sanitize_untrusted_text(str(row.get("text") or ""))
        return {
            "post_id": int(row.get("id") or row.get("post_id") or 0), "channel_id": channel_id,
            "identifier": identifier, "published_at": str(row.get("posted_at") or ""),
            "text": body[:16000], "text_truncated": len(body) > 16000,
            "source_link": source_link, "link": source_link, "warnings": list(warnings),
            **{k: int(row.get(k) or 0) for k in ("views", "reactions", "comments", "shares")},
        }

    async def read(self, conversation_id, channel_id: int, post_id: int):
        await self.require(conversation_id, channel_id)
        db = getattr(self.repository, "db", None)
        if db:
            row = await db.post_row(post_id)
        else:
            rows = await self.repository.performance_rows(channel_id, limit=1000000)
            row = next((r for r in rows if int(r.get("id") or r.get("post_id") or 0) == post_id), None)
        if not row or int(row.get("channel_id") or channel_id) != channel_id or row.get("is_deleted"):
            raise ConversationNotFound("Reference post not found.")
        return self.post(row, channel_id)
