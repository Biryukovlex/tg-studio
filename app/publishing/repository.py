"""Workspace-scoped durable publishing queue and audit trail."""
from __future__ import annotations
import json
import uuid
from datetime import datetime, timezone
from sqlalchemy import text
from .domain import PublishingError, make_parts, snapshot

UTC = timezone.utc


def public(row):
    result = dict(row)
    for key, value in list(result.items()):
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
    result.pop('lease_token', None)
    result.pop('lease_until', None)
    result.pop('idempotency_key', None)
    result['parts'] = [{k: v for k, v in p.items() if k not in {'random_id'}} for p in result.get('parts', [])]
    return result


def manifest(row):
    return {'id': str(row['id']), 'url': f"/studio/api/media/{row['id']}", 'filename': row['filename'], 'width': row['width'], 'height': row['height']}


class PublishingRepository:
    def __init__(self, db):
        self.db = db
        self.workspace_id = db.workspace_id

    async def channels(self):
        rows = (await self.db._execute('''SELECT c.id,c.identifier,c.title,c.active,c.chat_id,
            COALESCE(p.allowed,false) AS allowed, COALESCE(p.reason,'Waiting for Telegram worker to check permissions') AS reason,
            COALESCE(p.caption_limit,1024) AS caption_limit,p.checked_at,p.account_id
            FROM channels c LEFT JOIN publishing_channels p ON p.workspace_id=c.workspace_id AND p.channel_id=c.id
            WHERE c.workspace_id=:workspace_id ORDER BY c.title,c.id''')).mappings().all()
        return [public(row) for row in rows]

    async def set_permission(self, channel_id, allowed, reason, caption_limit=1024, account_id=None):
        await self.db._execute('''INSERT INTO publishing_channels(workspace_id,channel_id,allowed,reason,caption_limit,account_id)
            VALUES(:workspace_id,:channel_id,:allowed,:reason,:limit,:account_id) ON CONFLICT(workspace_id,channel_id)
            DO UPDATE SET allowed=:allowed,reason=:reason,caption_limit=:limit,account_id=:account_id,checked_at=now()''',
            {'channel_id': channel_id, 'allowed': allowed, 'reason': reason, 'limit': caption_limit, 'account_id': account_id})

    async def assets(self, ids, *, draft_id=None):
        if not ids:
            return []
        parsed = [uuid.UUID(str(item)) for item in ids]
        rows = (await self.db._execute('''SELECT id,draft_id,filename,width,height FROM studio_media
            WHERE workspace_id=:workspace_id AND id=ANY(:ids)''', {'ids': parsed})).mappings().all()
        found = {str(row['id']): row for row in rows}
        if len(set(parsed)) != len(parsed) or len(found) != len(parsed) or (draft_id and any(str(r['draft_id']) != str(draft_id) for r in rows)):
            raise PublishingError('One of these images is unavailable in this draft.')
        return [manifest(found[str(item)]) for item in parsed]

    async def asset(self, asset_id):
        return (await self.db._execute('SELECT * FROM studio_media WHERE workspace_id=:workspace_id AND id=:id', {'id': asset_id})).mappings().first()

    async def upload(self, draft_id, filename, image):
        asset_id = uuid.uuid4()
        async with self.db.sessions.session() as session:
            p = {'workspace_id': self.workspace_id, 'draft_id': draft_id}
            exists = (await session.execute(text('SELECT id FROM studio_drafts WHERE workspace_id=:workspace_id AND id=:draft_id FOR UPDATE'), p)).scalar_one_or_none()
            if not exists:
                raise PublishingError('Draft not found.', 'not_found', 404)
            await session.execute(text('SELECT id FROM workspaces WHERE id=:workspace_id FOR UPDATE'), p)
            size = (await session.execute(text('SELECT COALESCE(SUM(octet_length(content)),0) FROM studio_media WHERE workspace_id=:workspace_id'), p)).scalar_one()
            if size + len(image['content']) > 500 * 1024 * 1024:
                raise PublishingError('Workspace image storage is full (500 MB).', 'media_quota', 413)
            count = (await session.execute(text('SELECT count(*) FROM studio_media WHERE workspace_id=:workspace_id AND draft_id=:draft_id'), p)).scalar_one()
            if count >= 100:
                raise PublishingError('This draft has reached its image history limit (100 uploads).', 'media_quota', 413)
            row = (await session.execute(text('''INSERT INTO studio_media(id,workspace_id,draft_id,filename,content,content_type,width,height,sha256)
                VALUES(:id,:workspace_id,:draft_id,:filename,:content,:content_type,:width,:height,:sha256) RETURNING id,filename,width,height'''),
                {**p, **image, 'id': asset_id, 'filename': filename[:160]})).mappings().one()
            await session.commit()
        return manifest(row)

    async def create(self, payload, at, draft, media):
        try:
            request_id = uuid.UUID(payload.idempotency_key)
        except ValueError:
            raise PublishingError('Invalid request identifier.') from None
        async with self.db.sessions.session() as session:
            p = {'workspace_id': self.workspace_id, 'key': request_id}
            await session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:lock_key,0))'), {'lock_key': f'{self.workspace_id}:{request_id}'})
            existing = (await session.execute(text('SELECT * FROM scheduled_posts WHERE workspace_id=:workspace_id AND idempotency_key=:key'), p)).mappings().first()
            if existing:
                if str(existing['draft_id']) != payload.draft_id or existing['channel_id'] != payload.channel_id or existing['scheduled_at'] != at or existing['timezone'] != payload.timezone or existing['snapshot']['mode'] != payload.mode or existing['snapshot']['caption'] != (payload.caption if payload.mode == 'separate' else ''):
                    raise PublishingError('This request was already accepted with different details. Review the existing post in Calendar.', 'idempotency_conflict', 409)
                return public(existing)
            # Serialize requests against edits and check the revision at the transaction boundary.
            current = (await session.execute(text('SELECT * FROM studio_drafts WHERE workspace_id=:workspace_id AND id=:id FOR UPDATE'), {**p, 'id': uuid.UUID(draft['id'])})).mappings().first()
            if not current or current['revision'] != payload.expected_revision:
                raise PublishingError('The draft changed. Save or reload it before scheduling.', 'draft_conflict', 409)
            channel = (await session.execute(text('''SELECT c.*,p.allowed,p.caption_limit,p.checked_at,p.account_id FROM channels c
                LEFT JOIN publishing_channels p ON p.workspace_id=c.workspace_id AND p.channel_id=c.id
                WHERE c.workspace_id=:workspace_id AND c.id=:channel_id'''), {**p, 'channel_id': payload.channel_id})).mappings().first()
            if not channel or not channel['active']:
                raise PublishingError('Choose an active channel.')
            if not channel['allowed'] or not channel['account_id'] or not channel['checked_at'] or (datetime.now(UTC) - channel['checked_at']).total_seconds() > 180:
                raise PublishingError('Publishing permission is unavailable or stale. Wait for the Telegram worker to check the channel.', 'publishing_unavailable', 409)
            # Re-read image selection inside the same lock: clients cannot supply arbitrary assets.
            media = await self.assets(current['media_ids'], draft_id=draft['id'])
            value = snapshot(current['body'], media, payload.mode, payload.caption, channel['caption_limit'], current['working_title'])
            value['draft_version'], value['draft_revision'] = current['current_version'], current['revision']
            row = (await session.execute(text('''INSERT INTO scheduled_posts(id,workspace_id,channel_id,telegram_user_id,draft_id,conversation_id,
                snapshot,parts,scheduled_at,timezone,idempotency_key,action)
                VALUES(:id,:workspace_id,:channel_id,:account_id,:draft_id,:conversation_id,CAST(:snapshot AS jsonb),CAST(:parts AS jsonb),:at,:timezone,:key,'schedule') RETURNING *'''),
                {**p, 'id': uuid.uuid4(), 'channel_id': payload.channel_id, 'account_id': channel['account_id'], 'draft_id': current['id'], 'conversation_id': current['conversation_id'],
                 'snapshot': json.dumps(value), 'parts': json.dumps(make_parts(value)), 'at': at, 'timezone': payload.timezone})).mappings().one()
            await self.event(session, row['id'], 'queued', 'Confirmed by the user; waiting for Telegram acceptance.')
            await session.commit()
        return public(row)

    async def retry(self, post_id, revision, at, zone, value):
        async with self.db.sessions.session() as session:
            p = {'workspace_id': self.workspace_id, 'id': post_id}
            row = (await session.execute(text('SELECT * FROM scheduled_posts WHERE workspace_id=:workspace_id AND id=:id FOR UPDATE'), p)).mappings().first()
            if not row or row['revision'] != revision or row['status'] != 'failed' or row['action'] or any(part.get('scheduled_id') for part in row['parts']):
                raise PublishingError('This operation cannot safely be retried. Reload its Telegram state first.', 'schedule_conflict', 409)
            await session.execute(text("""UPDATE scheduled_posts SET snapshot=CAST(:value AS jsonb),scheduled_at=:at,timezone=:zone,
                status='queued',action='schedule',revision=revision+1,error='',next_attempt_at=now(),updated_at=now()
                WHERE workspace_id=:workspace_id AND id=:id"""), {**p, 'value': json.dumps(value), 'at': at, 'zone': zone})
            await self.event(session, post_id, 'queued', 'Retry explicitly confirmed by the user; no previous Telegram message IDs.')
            await session.commit()
        return await self.get(post_id)

    async def list(self, start, end, channel_id=None, status=None, search=''):
        query = '''SELECT s.*,c.title AS channel_title,c.identifier AS channel_identifier,c.chat_id AS channel_chat_id FROM scheduled_posts s
            JOIN channels c ON c.workspace_id=s.workspace_id AND c.id=s.channel_id
            WHERE s.workspace_id=:workspace_id AND s.scheduled_at >= :start AND s.scheduled_at < :end'''
        params = {'start': start, 'end': end, 'search': f'%{search[:200]}%'}
        if channel_id is not None:
            query += ' AND s.channel_id=:channel_id'
            params['channel_id'] = channel_id
        if status:
            query += ' AND s.status=:status'
            params['status'] = status
        if search:
            query += " AND (s.snapshot->>'body' ILIKE :search OR c.title ILIKE :search OR c.identifier ILIKE :search)"
        rows = (await self.db._execute(query + ' ORDER BY s.scheduled_at,s.id LIMIT 1001', params)).mappings().all()
        return [public(row) for row in rows[:1000]], len(rows) > 1000

    async def get(self, post_id):
        row = (await self.db._execute('''SELECT s.*,c.title AS channel_title,c.identifier AS channel_identifier,c.chat_id AS channel_chat_id FROM scheduled_posts s
            JOIN channels c ON c.workspace_id=s.workspace_id AND c.id=s.channel_id
            WHERE s.workspace_id=:workspace_id AND s.id=:id''', {'id': post_id})).mappings().first()
        return public(row) if row else None

    async def event(self, session, post_id, status, message=''):
        await session.execute(text('''INSERT INTO publishing_events(workspace_id,post_id,status,message)
            VALUES(:workspace_id,:id,:status,:message)'''), {'workspace_id': self.workspace_id, 'id': post_id, 'status': status, 'message': message[:500]})

    async def events(self, post_id):
        rows = (await self.db._execute('SELECT * FROM publishing_events WHERE workspace_id=:workspace_id AND post_id=:id ORDER BY created_at DESC,id DESC LIMIT 100', {'id': post_id})).mappings().all()
        return [public(row) for row in rows]

    async def command(self, post_id, revision, action, *, at=None, zone=None, value=None):
        async with self.db.sessions.session() as session:
            p = {'workspace_id': self.workspace_id, 'id': post_id}
            row = (await session.execute(text('SELECT * FROM scheduled_posts WHERE workspace_id=:workspace_id AND id=:id FOR UPDATE'), p)).mappings().first()
            if not row:
                raise PublishingError('Scheduled post not found.', 'not_found', 404)
            if action == 'cancel' and row['revision'] == revision and row['action'] == 'schedule' and row['lease_token'] is None and not any(part.get('scheduled_id') for part in row['parts']):
                await session.execute(text("UPDATE scheduled_posts SET status='cancelled',action=NULL,revision=revision+1,updated_at=now() WHERE workspace_id=:workspace_id AND id=:id"), p)
                await self.event(session, post_id, 'cancelled', 'Cancelled by the user before transfer to Telegram.')
                await session.commit()
                return await self.get(post_id)
            if row['revision'] != revision or row['action'] or row['status'] not in {'scheduled', 'needs_review'}:
                raise PublishingError('This post changed or is being processed. Reload it first.', 'schedule_conflict', 409)
            if action == 'update' and row['status'] != 'scheduled':
                raise PublishingError('Resolve the uncertain Telegram state before editing this post.', 'needs_review', 409)
            if action == 'cancel' and (not row['parts'] or any(not part.get('scheduled_id') or part.get('published_id') for part in row['parts'])):
                raise PublishingError('Not all Telegram message IDs are known. Check the Telegram schedule queue first.', 'needs_review', 409)
            pending = 'cancelling' if action == 'cancel' else 'updating'
            await session.execute(text('''UPDATE scheduled_posts SET action=:action,status=:status,pending_at=:at,pending_timezone=:zone,
                pending_snapshot=CAST(:snapshot AS jsonb),revision=revision+1,error='',updated_at=now(),next_attempt_at=now()
                WHERE workspace_id=:workspace_id AND id=:id'''), {**p, 'action': action, 'status': pending, 'at': at, 'zone': zone, 'snapshot': json.dumps(value) if value else None})
            await self.event(session, post_id, pending, 'Confirmed by the user.')
            await session.commit()
        return await self.get(post_id)

    async def claim(self):
        token = uuid.uuid4()
        async with self.db.sessions.session() as session:
            p = {'workspace_id': self.workspace_id, 'token': token}
            # A dead lease after a network send is ambiguous. Never blindly retry it.
            expired = (await session.execute(text('''UPDATE scheduled_posts SET status='needs_review',action=NULL,lease_token=NULL,
                lease_until=NULL,error='Worker interrupted. Check the Telegram schedule queue before retrying.',updated_at=now(),revision=revision+1
                WHERE workspace_id=:workspace_id AND lease_until < now() AND action IS NOT NULL RETURNING id'''), p)).scalars().all()
            for post_id in expired:
                await self.event(session, post_id, 'needs_review', 'Worker interrupted; no automatic resend.')
            row = (await session.execute(text('''SELECT * FROM scheduled_posts WHERE workspace_id=:workspace_id AND action IS NOT NULL
                AND lease_token IS NULL AND next_attempt_at <= now() ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1'''), p)).mappings().first()
            if row:
                await session.execute(text('''UPDATE scheduled_posts SET lease_token=:token,lease_until=now()+interval '10 minutes',
                    status=CASE WHEN action='schedule' THEN 'transferring' ELSE status END,updated_at=now() WHERE workspace_id=:workspace_id AND id=:id'''), {**p, 'id': row['id']})
            await session.commit()
        return ({**dict(row), 'lease_token': token} if row else None)

    async def resolve(self, post_id, revision):
        """Record the owner's manual queue cleanup, without making a Telegram claim."""
        async with self.db.sessions.session() as session:
            p = {'workspace_id': self.workspace_id, 'id': post_id, 'revision': revision}
            row = (await session.execute(text('''UPDATE scheduled_posts SET status='cancelled',error='',
                revision=revision+1,updated_at=now() WHERE workspace_id=:workspace_id AND id=:id
                AND revision=:revision AND status='needs_review' AND action IS NULL RETURNING id'''), p)).scalar_one_or_none()
            if not row:
                raise PublishingError('This post changed or no longer needs review. Reload it first.', 'schedule_conflict', 409)
            await self.event(session, post_id, 'cancelled', 'Manually resolved by the owner after checking Telegram and removing all remaining scheduled messages. Earlier publication is not asserted or reversed.')
            await session.commit()
        return await self.get(post_id)

    async def save_parts(self, row, parts):
        await self.db._execute('''UPDATE scheduled_posts SET parts=CAST(:parts AS jsonb),lease_until=now()+interval '10 minutes'
            WHERE workspace_id=:workspace_id AND id=:id AND lease_token=:token''', {'id': row['id'], 'token': row['lease_token'], 'parts': json.dumps(parts)})

    async def finish(self, row, status, error='', *, value=None, at=None, zone=None):
        async with self.db.sessions.session() as session:
            p = {'workspace_id': self.workspace_id, 'id': row['id'], 'token': row['lease_token'], 'status': status, 'error': error[:500],
                 'snapshot': json.dumps(value) if value else None, 'at': at, 'zone': zone}
            result = await session.execute(text('''UPDATE scheduled_posts SET status=:status,error=:error,action=NULL,lease_token=NULL,lease_until=NULL,
                snapshot=COALESCE(CAST(:snapshot AS jsonb),snapshot),scheduled_at=COALESCE(:at,scheduled_at),timezone=COALESCE(:zone,timezone),
                pending_snapshot=NULL,pending_at=NULL,pending_timezone=NULL,revision=revision+1,updated_at=now()
                WHERE workspace_id=:workspace_id AND id=:id AND lease_token=:token RETURNING id'''), p)
            if result.scalar_one_or_none():
                await self.event(session, row['id'], status, error or 'Confirmed by Telegram.')
            await session.commit()

    async def reconcile_rows(self):
        rows = (await self.db._execute('''SELECT s.*,c.chat_id,c.identifier FROM scheduled_posts s JOIN channels c
            ON c.workspace_id=s.workspace_id AND c.id=s.channel_id WHERE s.workspace_id=:workspace_id
            AND s.action IS NULL AND s.status IN ('scheduled','needs_review') ORDER BY s.scheduled_at LIMIT 1000''')).mappings().all()
        return [dict(row) for row in rows]

    async def confirmed_update(self, row, *, parts=None, status=None, value=None, at=None, error=''):
        async with self.db.sessions.session() as session:
            p = {'workspace_id': self.workspace_id, 'id': row['id'], 'revision': row['revision'], 'status': status or row['status'],
                 'parts': json.dumps(parts if parts is not None else row['parts']), 'snapshot': json.dumps(value or row['snapshot']), 'at': at or row['scheduled_at'], 'error': error}
            result = await session.execute(text('''UPDATE scheduled_posts SET parts=CAST(:parts AS jsonb),snapshot=CAST(:snapshot AS jsonb),scheduled_at=:at,
                status=:status,error=:error,updated_at=now(),revision=revision+1 WHERE workspace_id=:workspace_id AND id=:id
                AND revision=:revision AND action IS NULL RETURNING id'''), p)
            if result.scalar_one_or_none():
                await self.event(session, row['id'], p['status'], error or 'Telegram state reconciled.')
            await session.commit()
