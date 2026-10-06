"""Human-confirmed scheduling API; web processes never acquire Telethon."""
from __future__ import annotations
import asyncio
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartParser, MultiPartException
from pydantic import ValidationError
from ..web.dependencies import require_auth, require_csrf, csrf_token
from ..studio.markdown import validate_markdown_body
from ..studio.drafts import DraftValidationError
from .domain import ScheduleInput, ScheduleEdit, PublishingError, local_to_utc, snapshot
from .repository import PublishingRepository
from .media import prepare_image, MAX_UPLOAD

router = APIRouter(dependencies=[Depends(require_auth)])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / 'web' / 'templates'))


def repo(request):
    return PublishingRepository(request.app.state.db)


def parsed_id(value):
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(404, 'Not found') from None


def error(exc):
    return JSONResponse({'error': {'code': exc.code, 'message': str(exc), 'retryable': False}}, status_code=exc.status)


@router.get('/calendar')
async def calendar_page(request: Request):
    context = require_auth(request)
    from ..web.routes import static_asset_version
    return templates.TemplateResponse(request, 'calendar.html', {'request': request, 'csrf_token': csrf_token(request), 'can_manage_settings': context.role == 'owner', 'app_name': 'TGhost', 'asset_version': static_asset_version()})


@router.get('/studio/api/publishing/channels')
async def channels(request: Request):
    require_auth(request)
    return {'channels': await repo(request).channels()}


@router.post('/studio/api/drafts/{draft_id}/media', dependencies=[Depends(require_csrf)])
async def upload(request: Request, draft_id: str):
    require_auth(request)
    require_csrf(request)
    draft_id = parsed_id(draft_id)
    form = None
    try:
        # Authorize before parsing, and cap bytes while streaming, including
        # requests without Content-Length. A large upload cannot fill temp disk.
        async def bounded_stream():
            total = 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_UPLOAD + 65536:
                    raise MultiPartException('Image request exceeds 10 MB.')
                yield chunk
        form = await MultiPartParser(request.headers, bounded_stream(), max_files=1, max_fields=0).parse()
        file = form.get('file')
        if not isinstance(file, UploadFile):
            raise PublishingError('Choose one image file.')
        content = await file.read(MAX_UPLOAD + 1)
        image = await asyncio.to_thread(prepare_image, content)
        result = await repo(request).upload(draft_id, str(file.filename or 'image.jpg'), image)
        return {'media': result}
    except PublishingError as exc:
        return error(exc)
    except MultiPartException:
        return error(PublishingError('Upload one image smaller than 10 MB.', 'invalid_upload', 413))
    finally:
        if form is not None:
            await form.close()


@router.get('/studio/api/media/{asset_id}')
async def media(request: Request, asset_id: str):
    require_auth(request)
    row = await repo(request).asset(parsed_id(asset_id))
    if not row:
        raise HTTPException(404, 'Image not found')
    return Response(bytes(row['content']), media_type=row['content_type'], headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff', 'Content-Disposition': 'inline; filename="image.jpg"'})


@router.get('/studio/api/drafts/{draft_id}/media')
async def draft_media(request: Request, draft_id: str, version: int | None = None):
    require_auth(request)
    draft = await request.app.state.studio_repository.get_draft(parsed_id(draft_id))
    if not draft:
        raise HTTPException(404, 'Draft not found')
    ids = draft.get('media_ids', [])
    if version is not None:
        versions = await request.app.state.studio_repository.list_draft_versions(draft_id=parsed_id(draft_id))
        selected = next((item for item in versions if item['version'] == version), None)
        if not selected:
            raise HTTPException(404, 'Version not found')
        ids = selected.get('media_ids', [])
    return {'media': await repo(request).assets(ids)}


@router.get('/studio/api/publishing/drafts')
async def draft_choices(request: Request):
    require_auth(request)
    rows = (await request.app.state.db._execute('''SELECT d.id,d.conversation_id,d.channel_id,d.working_title,d.body,d.revision,d.media_ids,
        c.title AS channel_title,c.identifier AS channel_identifier FROM studio_drafts d JOIN channels c
        ON c.workspace_id=d.workspace_id AND c.id=d.channel_id WHERE d.workspace_id=:workspace_id
        AND d.status <> 'archived' ORDER BY d.updated_at DESC,d.id LIMIT 200''')).mappings().all()
    return {'drafts': [{**dict(row), 'id': str(row['id']), 'conversation_id': str(row['conversation_id'])} for row in rows]}


@router.post('/studio/api/publishing/posts', dependencies=[Depends(require_csrf)])
async def create(request: Request):
    require_auth(request)
    require_csrf(request)
    try:
        payload = ScheduleInput.model_validate(await request.json())
        at = local_to_utc(payload.local_datetime, payload.timezone, payload.fold)
        draft = await request.app.state.studio_repository.get_draft(parsed_id(payload.draft_id))
        if not draft:
            raise HTTPException(404, 'Draft not found')
        return {'post': await repo(request).create(payload, at, draft, [])}
    except PublishingError as exc:
        return error(exc)
    except (ValidationError, ValueError, TypeError):
        return error(PublishingError('Scheduling details are invalid. Explicit confirmation is required.'))


@router.get('/studio/api/publishing/posts')
async def posts(request: Request, start: str, end: str, channel_id: int | None = None, status: str | None = None, search: str = ''):
    require_auth(request)
    try:
        lower, upper = datetime.fromisoformat(start.replace('Z', '+00:00')), datetime.fromisoformat(end.replace('Z', '+00:00'))
        if lower.tzinfo is None or upper.tzinfo is None or upper <= lower or upper-lower > timedelta(days=370):
            raise ValueError()
    except ValueError:
        return error(PublishingError('Choose a timezone-aware range no longer than a year.'))
    values, truncated = await repo(request).list(lower, upper, channel_id, status, search)
    return {'posts': values, 'truncated': truncated}


@router.get('/studio/api/publishing/posts/{post_id}')
async def post(request: Request, post_id: str):
    require_auth(request)
    result = await repo(request).get(parsed_id(post_id))
    if not result:
        raise HTTPException(404, 'Scheduled post not found')
    return {'post': result, 'events': await repo(request).events(parsed_id(post_id))}


@router.patch('/studio/api/publishing/posts/{post_id}', dependencies=[Depends(require_csrf)])
async def edit(request: Request, post_id: str):
    require_auth(request)
    require_csrf(request)
    try:
        payload = ScheduleEdit.model_validate(await request.json())
        post_id = parsed_id(post_id)
        current = await repo(request).get(post_id)
        if not current:
            raise HTTPException(404, 'Scheduled post not found')
        at = local_to_utc(payload.local_datetime, payload.timezone, payload.fold)
        value = current['snapshot']
        body = payload.body if payload.body is not None else value['body']
        validate_markdown_body(body)
        channel = next((c for c in await repo(request).channels() if c['id'] == current['channel_id']), None)
        if not channel or not channel['allowed']:
            raise PublishingError('Publishing permission is unavailable.', 'publishing_unavailable', 409)
        updated = snapshot(body, value['media'], value['mode'], payload.caption if payload.caption is not None else value['caption'], channel['caption_limit'], value['title'])
        if current['status'] == 'failed':
            return {'post': await repo(request).retry(post_id, payload.expected_revision, at, payload.timezone, updated)}
        return {'post': await repo(request).command(post_id, payload.expected_revision, 'update', at=at, zone=payload.timezone, value=updated)}
    except PublishingError as exc:
        return error(exc)
    except (ValidationError, DraftValidationError, ValueError, TypeError):
        return error(PublishingError('Post details are invalid. Use supported Telegram formatting.'))


@router.post('/studio/api/publishing/posts/{post_id}/cancel', dependencies=[Depends(require_csrf)])
async def cancel(request: Request, post_id: str):
    require_auth(request)
    require_csrf(request)
    try:
        payload = await request.json()
        if not isinstance(payload, dict) or payload.get('confirm') is not True or type(payload.get('expected_revision')) is not int:
            raise PublishingError('Confirm cancellation explicitly.')
        return {'post': await repo(request).command(parsed_id(post_id), payload['expected_revision'], 'cancel')}
    except PublishingError as exc:
        return error(exc)
    except (ValueError, TypeError):
        return error(PublishingError('Cancellation details are invalid.'))


@router.post('/studio/api/publishing/posts/{post_id}/resolve', dependencies=[Depends(require_csrf)])
async def resolve(request: Request, post_id: str):
    context = require_auth(request)
    require_csrf(request)
    try:
        if context.role != 'owner':
            raise PublishingError('Only the workspace owner can confirm a manual resolution.', 'owner_required', 403)
        payload = await request.json()
        if not isinstance(payload, dict) or payload.get('confirm') is not True or payload.get('confirmed_removed_in_telegram') is not True or type(payload.get('expected_revision')) is not int:
            raise PublishingError('Check Telegram and confirm removal of every remaining scheduled message first.')
        return {'post': await repo(request).resolve(parsed_id(post_id), payload['expected_revision'])}
    except PublishingError as exc:
        return error(exc)
    except (ValueError, TypeError):
        return error(PublishingError('Manual resolution details are invalid.'))
