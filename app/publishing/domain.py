"""Scheduling contracts. No LLM and no network calls at this boundary."""
from __future__ import annotations
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from telethon.extensions import html
from ..studio.markdown import render_markdown_html
from ..studio.drafts import telegram_character_count

UTC = timezone.utc
MIN_LEAD_SECONDS = 120
MAX_LEAD_DAYS = 365
MAX_IMAGES = 10


class PublishingError(ValueError):
    def __init__(self, message: str, code: str = 'invalid_schedule', status: int = 422):
        super().__init__(message)
        self.code, self.status = code, status


class ScheduleInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    draft_id: str
    expected_revision: int = Field(ge=1)
    channel_id: int
    local_datetime: str
    timezone: str = Field(max_length=80)
    fold: Literal[0, 1] | None = None
    mode: Literal['caption', 'separate'] = 'caption'
    caption: str = Field(default='', max_length=4000)
    confirm: Literal[True]
    idempotency_key: str

    @field_validator('confirm', mode='before')
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError('Explicit confirmation is required.')
        return value


class ScheduleEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)
    local_datetime: str
    timezone: str = Field(max_length=80)
    fold: Literal[0, 1] | None = None
    body: str | None = Field(default=None, max_length=32000)
    caption: str | None = Field(default=None, max_length=4000)
    confirm: Literal[True]

    @field_validator('confirm', mode='before')
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError('Explicit confirmation is required.')
        return value


def local_to_utc(value: str, zone: str, fold: int | None = None, *, now: datetime | None = None) -> datetime:
    try:
        tz = ZoneInfo(zone)
        local = datetime.fromisoformat(value)
    except (ValueError, ZoneInfoNotFoundError):
        raise PublishingError('Choose a valid date, time and IANA timezone.') from None
    if local.tzinfo is not None:
        raise PublishingError('Supply a local date and time without an offset.')
    candidates = []
    for f in (0, 1):
        aware = local.replace(tzinfo=tz, fold=f)
        result = aware.astimezone(UTC)
        if result.astimezone(tz).replace(tzinfo=None) == local and result not in candidates:
            candidates.append(result)
    if not candidates:
        raise PublishingError('This local time does not exist because of daylight saving time. Choose another time.')
    if len(candidates) > 1 and fold is None:
        raise PublishingError('This local time occurs twice. Choose the first or second occurrence.', 'ambiguous_time')
    result = local.replace(tzinfo=tz, fold=fold or 0).astimezone(UTC)
    validate_future(result, now=now)
    return result


def validate_future(value: datetime, *, now: datetime | None = None):
    current = now or datetime.now(UTC)
    if value < current + timedelta(seconds=MIN_LEAD_SECONDS):
        raise PublishingError('Choose a time at least two minutes in the future. Nothing was published.')
    if value > current + timedelta(days=MAX_LEAD_DAYS):
        raise PublishingError('Choose a date within the next year.')


def telegram_text(body: str):
    # Our renderer emits <br> for clipboard HTML; Telegram expects real LF.
    return html.parse(render_markdown_html(body).replace('<br>', '\n'))


def snapshot(body: str, media: list[dict], mode: str, caption: str, caption_limit: int, title: str = '') -> dict:
    text, _ = telegram_text(body)
    caption_text, _ = telegram_text(caption)
    if not text.strip() and not media:
        raise PublishingError('Write a post or add an image first.')
    if telegram_character_count(text) > 4096:
        raise PublishingError('Shorten the post to 4096 Telegram characters.')
    if len(media) > MAX_IMAGES:
        raise PublishingError('An album supports up to ten images.')
    if media and mode == 'caption' and telegram_character_count(text) > caption_limit:
        raise PublishingError('The post exceeds the photo caption limit. Shorten it or choose images + a separate text message.', 'caption_too_long')
    if media and mode == 'separate' and telegram_character_count(caption_text) > caption_limit:
        raise PublishingError('Shorten the image caption.')
    if mode == 'separate' and not text.strip():
        raise PublishingError('A separate text message cannot be empty.')
    return {'body': body, 'media': media, 'mode': mode, 'caption': caption if mode == 'separate' else '', 'title': title}


def make_parts(value: dict) -> list[dict]:
    media = value.get('media', [])
    parts = [{'kind': 'photo', 'asset_id': item['id'], 'random_id': secrets.randbits(63) or 1} for item in media]
    if not media or value['mode'] == 'separate':
        parts.append({'kind': 'text', 'random_id': secrets.randbits(63) or 1})
    return parts
