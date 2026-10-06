import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ScheduleDialog, TelegramPreview, zonedInput } from './publishing';
import { calendarDays, addDays } from './CalendarPage';
import { api, type Draft } from './api';
vi.mock('./api', async () => ({ ...(await vi.importActual('./api')), api: vi.fn(), csrfToken: () => 'synthetic-csrf' }));
const draft = { id: 'd1', channel_id: 1, body: 'A **synthetic** post', current_version: 3, revision: 5, working_title: 'Fixture' } as Draft;
const channel = { id: 1, title: 'Fixture channel', identifier: '@fixture', allowed: true, active: true, caption_limit: 1024 };
const mockedApi = vi.mocked(api);
beforeEach(() => {
  vi.clearAllMocks();
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  mockedApi.mockImplementation(async (url: string) => {
    if (url.endsWith('/channels')) return { channels: [channel] };
    if (url.endsWith('/media')) return { media: [] };
    return { post: { id: 'p1', status: 'queued' } };
  });
});
afterEach(() => { vi.restoreAllMocks(); });
describe('Calendar dates', () => {
  it('creates Monday-first month boundaries across a year', () => {
    const days = calendarDays('2027-01-20', 'month');
    expect(days).toHaveLength(42);
    expect(days[0]).toBe('2026-12-28');
    expect(days[41]).toBe('2027-02-07');
    expect(calendarDays('2026-10-06', 'week')).toEqual(['2026-10-05','2026-10-06','2026-10-07','2026-10-08','2026-10-09','2026-10-10','2026-10-11']);
    expect(addDays('2028-02-28', 1)).toBe('2028-02-29');
  });
  it('formats the selected timezone independently from browser locale', () => {
    expect(zonedInput(new Date('2026-01-02T09:00:00Z'), 'Europe/Madrid')).toBe('2026-01-02T10:00');
  });
});
describe('Explicit human scheduling', () => {
  it('loads a preview without publishing and sends the exact draft revision only on confirmation', async () => {
    const onScheduled = vi.fn();
    render(<ScheduleDialog draft={draft} initialDate="2026-12-20T10:30" onClose={vi.fn()} onScheduled={onScheduled} />);
    await screen.findAllByText('Fixture channel');
    expect(mockedApi.mock.calls.some(([url]) => url === '/studio/api/publishing/posts')).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: 'Confirm schedule' }));
    await waitFor(() => expect(onScheduled).toHaveBeenCalled());
    const call = mockedApi.mock.calls.find(([url]) => url === '/studio/api/publishing/posts');
    const body = JSON.parse(call![1]!.body as string);
    expect(body).toMatchObject({ draft_id: 'd1', expected_revision: 5, channel_id: 1, local_datetime: '2026-12-20T10:30', confirm: true });
    expect(body.idempotency_key).toBeTruthy();
  });
  it('blocks a channel without publishing rights even though it is available for analytics', async () => {
    mockedApi.mockImplementation(async (url: string) => url.endsWith('/channels') ? { channels: [{ ...channel, allowed: false, reason: 'No publishing permission' }] } : { media: [] });
    render(<ScheduleDialog draft={draft} onClose={vi.fn()} onScheduled={vi.fn()} />);
    await screen.findByText('No publishing permission');
    expect((screen.getByRole('button', { name: 'Confirm schedule' }) as HTMLButtonElement).disabled).toBe(true);
  });
  it('previews an album and a separate text message without breaking or truncating its body', () => {
    const { container } = render(<TelegramPreview channel="Fixture" value={{ title: '', body: 'A full **text** message', caption: 'Caption', mode: 'separate', media: [{ id: '1', url: '/studio/api/media/1', filename: 'Fixture', width: 160, height: 120 }, { id: '2', url: '/studio/api/media/2', filename: 'Fixture 2', width: 160, height: 120 }] }} />);
    expect(container.querySelectorAll('article')).toHaveLength(2);
    expect(container.querySelectorAll('img')).toHaveLength(2);
    expect(screen.getByText('Caption')).toBeTruthy();
    expect(screen.getByText(/Then a separate text message/)).toBeTruthy();
  });
});
