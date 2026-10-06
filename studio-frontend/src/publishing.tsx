import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { api, csrfToken, type Draft } from './api';
import { useDialogFocusTrap } from './dialogFocus';
import { htmlFromMarkdown } from './markdownCopy';
import './publishing.css';

export type Media = { id: string; url: string; filename: string; width: number; height: number };
export type PublishingChannel = { id: number; title: string; identifier: string; active: boolean; allowed: boolean; reason: string; caption_limit: number; checked_at: string | null };
export type ScheduledPost = {
  id: string; channel_id: number; channel_title: string; channel_identifier: string; channel_chat_id?: number | null; draft_id: string | null; conversation_id: string | null;
  snapshot: { body: string; title: string; caption: string; mode: 'caption' | 'separate'; media: Media[]; external_body_html?: string };
  scheduled_at: string; timezone: string; status: string; error: string; revision: number;
  parts: { kind: string; scheduled_id?: number; published_id?: number; cancelled?: boolean }[];
};
export const STATUS_LABELS: Record<string, string> = { queued: 'Queued', transferring: 'Transferring', scheduled: 'Scheduled', updating: 'Updating', cancelling: 'Cancelling', published: 'Published', cancelled: 'Cancelled', failed: 'Failed', needs_review: 'Needs review' };
export function timezonePreference() { try { return localStorage.getItem('calendar-timezone') || Intl.DateTimeFormat().resolvedOptions().timeZone; } catch { return 'Europe/Madrid'; } }
export function zonedInput(value: Date, zone: string) {
  const p = new Intl.DateTimeFormat('en-CA', { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(value);
  const get = (key: string) => p.find(i => i.type === key)?.value;
  return `${get('year')}-${get('month')}-${get('day')}T${get('hour')}:${get('minute')}`;
}
export function zoneOptions(current: string) { return [...new Set([current, 'Europe/Madrid', 'Europe/Moscow', 'Europe/London', 'Europe/Berlin', 'America/New_York', 'America/Los_Angeles', 'Asia/Dubai', 'Asia/Tbilisi', 'UTC', ...Intl.supportedValuesOf('timeZone')])]; }
const writeHeaders = () => ({ 'content-type': 'application/json', 'x-csrf-token': csrfToken() });

export function ImageAttachments({ draftId, ids, version, disabled, onChange, onBusyChange }: { draftId: string; ids: string[]; version?: number; disabled: boolean; onChange: (ids: string[]) => void; onBusyChange?: (busy: boolean) => void }) {
  const [media, setMedia] = useState<Media[]>([]);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState('');
  useEffect(() => { onBusyChange?.(progress !== null); return () => onBusyChange?.(false); }, [progress, onBusyChange]);
  const input = useRef<HTMLInputElement>(null);
  const replacing = useRef<number | null>(null);
  const cache = useRef(new Map<string, Media>());
  const activeScope = useRef(draftId);
  useLayoutEffect(() => { activeScope.current = draftId; }, [draftId]);
  useEffect(() => {
    let current = true;
    void api<{ media: Media[] }>(`/studio/api/drafts/${draftId}/media${version ? `?version=${version}` : ''}`).then(r => {
      if (!current) return;
      r.media.forEach(m => cache.current.set(m.id, m));
      setMedia(ids.map(id => cache.current.get(id)).filter((m): m is Media => !!m));
    }).catch(e => { if (current) setError(e.message); });
    return () => { current = false; };
  }, [draftId, version, ids.join(',')]);
  const upload = async (files: File[]) => {
    if (disabled || progress !== null || !files.length) return;
    const scope = draftId;
    const replace = replacing.current;
    replacing.current = null;
    if ((replace === null ? ids.length : ids.length - 1) + files.length > 10) { setError('An album supports up to ten images.'); return; }
    const next = [...ids];
    setProgress(0); setError('');
    try {
      for (let index = 0; index < files.length; index++) {
        const file = files[index];
        if (file.size > 10 * 1024 * 1024) throw new Error('Use images smaller than 10 MB each.');
        const item = await new Promise<Media>((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open('POST', `/studio/api/drafts/${scope}/media`);
          xhr.setRequestHeader('x-csrf-token', csrfToken());
          xhr.setRequestHeader('accept', 'application/json');
          xhr.upload.onprogress = event => { if (activeScope.current === scope) setProgress(Math.round(((index + (event.lengthComputable ? event.loaded / event.total : 0)) / files.length) * 100)); };
          xhr.onerror = () => reject(new Error('Image upload failed. Try again.'));
          xhr.onload = () => { try { const payload = JSON.parse(xhr.responseText); if (xhr.status >= 200 && xhr.status < 300) resolve(payload.media); else reject(new Error(payload.error?.message || 'Image upload failed.')); } catch { reject(new Error('Image upload failed. Sign in and try again.')); } };
          const data = new FormData(); data.append('file', file); xhr.send(data);
        });
        if (activeScope.current !== scope) return;
        cache.current.set(item.id, item);
        if (replace !== null && index === 0) next[replace] = item.id; else next.push(item.id);
        // Preserve every successfully uploaded image if a later upload fails.
        onChange([...next]);
        setMedia(next.map(id => cache.current.get(id)).filter((m): m is Media => !!m));
      }
    } catch (e) { if (activeScope.current === scope) setError(e instanceof Error ? e.message : 'Upload failed.'); }
    finally { if (activeScope.current === scope) setProgress(null); }
  };
  const move = (index: number, delta: number) => { const next = [...ids]; [next[index], next[index + delta]] = [next[index + delta], next[index]]; onChange(next); };
  return <section className="attachment-editor" aria-label="Post images" onDragOver={e => { if (!disabled) e.preventDefault(); }} onDrop={e => { e.preventDefault(); void upload([...e.dataTransfer.files]); }} onPaste={e => { const files = [...e.clipboardData.files]; if (files.length) { e.preventDefault(); void upload(files); } }} tabIndex={0}>
    <div className="attachment-heading"><strong>Images <span>{ids.length} / 10</span></strong><button type="button" disabled={disabled || progress !== null || ids.length >= 10} onClick={() => { replacing.current = null; input.current?.click(); }}><i className="mgc mgc-pic-core-regular" aria-hidden="true" /> Add images</button></div>
    <input ref={input} hidden type="file" accept="image/jpeg,image/png,image/webp" multiple onChange={e => { void upload([...(e.target.files || [])]); e.target.value = ''; }} />
    {media.length ? <ol className="attachment-grid">{media.map((m, index) => <li key={m.id}><img src={m.url} alt={m.filename} /><span className="attachment-number">{index + 1}</span><div className="attachment-actions"><button type="button" aria-label={`Move image ${index + 1} earlier`} disabled={disabled || progress !== null || index === 0} onClick={() => move(index, -1)}>←</button><button type="button" aria-label={`Move image ${index + 1} later`} disabled={disabled || progress !== null || index === ids.length - 1} onClick={() => move(index, 1)}>→</button><button type="button" aria-label={`Replace image ${index + 1}`} disabled={disabled || progress !== null} onClick={() => { replacing.current = index; input.current?.click(); }}>↻</button><button type="button" aria-label={`Remove image ${index + 1}`} disabled={disabled || progress !== null} onClick={() => onChange(ids.filter(id => id !== m.id))}>×</button></div></li>)}</ol> : <p className="attachment-hint">Drop images here or paste from your clipboard. JPEG, PNG, WebP · up to 10 MB each.</p>}
    {progress !== null && <div role="status"><progress max={100} value={progress} aria-label="Uploading images" /> Uploading {progress}%</div>}
    {error && <p className="publishing-error" role="alert">{error}</p>}
  </section>;
}

export function TelegramPreview({ value, channel }: { value: ScheduledPost['snapshot']; channel: string }) {
  const html = value.external_body_html || htmlFromMarkdown(value.body);
  return <div className="telegram-preview"><p className="preview-label">Telegram preview</p><article className="telegram-post"><strong className="telegram-channel">{channel || 'Your channel'}</strong>{value.media.length > 0 && <div className={`telegram-images ${value.media.length === 1 ? 'is-single' : ''}`}>{value.media.map((m, i) => <img key={m.id} src={m.url} alt={`Image ${i + 1}`} />)}</div>}{value.media.length > 0 && value.mode === 'separate' ? value.caption && <div className="telegram-copy" dangerouslySetInnerHTML={{ __html: htmlFromMarkdown(value.caption) }} /> : <div className="telegram-copy" dangerouslySetInnerHTML={{ __html: html }} />}<div className="telegram-meta">Scheduled post</div></article>{value.media.length > 0 && value.mode === 'separate' && <><p className="preview-sequence">Then a separate text message, one second later</p><article className="telegram-post"><strong className="telegram-channel">{channel || 'Your channel'}</strong><div className="telegram-copy" dangerouslySetInnerHTML={{ __html: html }} /></article></>}</div>;
}

export function ScheduleDialog({ draft, initialDate, onClose, onScheduled }: { draft: Draft; initialDate?: string; onClose: () => void; onScheduled: (post: ScheduledPost) => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useDialogFocusTrap(ref);
  const [channels, setChannels] = useState<PublishingChannel[]>([]);
  const [media, setMedia] = useState<Media[]>([]);
  const [channelId, setChannelId] = useState(draft.channel_id);
  const [zone, setZone] = useState(timezonePreference);
  const [date, setDate] = useState(() => initialDate || zonedInput(new Date(Date.now() + 3600_000), zone));
  const [fold, setFold] = useState<string>('');
  const [mode, setMode] = useState<'caption' | 'separate'>('caption');
  const [caption, setCaption] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState('edit');
  const requestId = useRef(crypto.randomUUID());
  useEffect(() => { ref.current?.showModal(); }, []);
  useEffect(() => {
    let active = true;
    const load = async () => { try { const [c, m] = await Promise.all([api<{ channels: PublishingChannel[] }>('/studio/api/publishing/channels'), api<{ media: Media[] }>(`/studio/api/drafts/${draft.id}/media`)]); if (active) { setChannels(c.channels); setMedia(m.media); } } catch (e) { if (active) setError(e instanceof Error ? e.message : 'Could not load publishing details.'); } };
    void load(); const poll = window.setInterval(() => { void load(); }, 5000); return () => { active = false; clearInterval(poll); };
  }, [draft.id]);
  const selected = channels.find(c => c.id === channelId);
  const submit = async () => {
    if (busy || !selected?.allowed) return;
    setBusy(true); setError('');
    try {
      const result = await api<{ post: ScheduledPost }>('/studio/api/publishing/posts', { method: 'POST', headers: writeHeaders(), body: JSON.stringify({ draft_id: draft.id, expected_revision: draft.revision, channel_id: channelId, local_datetime: date, timezone: zone, fold: fold === '' ? null : Number(fold), mode, caption, confirm: true, idempotency_key: requestId.current }) });
      onScheduled(result.post);
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not schedule this post.'); }
    finally { setBusy(false); }
  };
  return <dialog ref={ref} className="publishing-dialog" aria-labelledby="schedule-title" onCancel={e => { if (e.target !== e.currentTarget) return; e.preventDefault(); if (!busy) onClose(); }}>
    <header className="publishing-dialog-header"><div><p className="publishing-eyebrow">Publish with Telegram</p><h2 id="schedule-title">Schedule post</h2></div><button type="button" className="publishing-icon" aria-label="Close scheduling" onClick={onClose} disabled={busy}>×</button></header>
    <div className="publishing-mobile-tabs"><button type="button" aria-pressed={tab === 'edit'} onClick={() => setTab('edit')}>Details</button><button type="button" aria-pressed={tab === 'preview'} onClick={() => setTab('preview')}>Preview</button></div>
    <div className={`publishing-dialog-body tab-${tab}`}><section className="schedule-fields"><label>Channel<select value={channelId} onChange={e => setChannelId(Number(e.target.value))} disabled={busy}>{channels.map(c => <option key={c.id} value={c.id} disabled={!c.allowed || !c.active}>{c.title || c.identifier}{!c.allowed ? ' · No publishing access' : ''}</option>)}</select></label>{!selected?.allowed && <p className="publishing-note" role="status">{selected?.reason || 'Checking publishing permissions…'}</p>}
      <label>Date and time<input type="datetime-local" value={date} onChange={e => setDate(e.target.value)} disabled={busy} required /></label><label>Timezone<select value={zone} onChange={e => { setZone(e.target.value); setFold(''); }} disabled={busy}>{zoneOptions(zone).map(z => <option key={z}>{z}</option>)}</select></label>
      {error.includes('occurs twice') && <label>Daylight saving time<select value={fold} onChange={e => setFold(e.target.value)}><option value="">Choose an occurrence</option><option value="0">First occurrence</option><option value="1">Second occurrence</option></select></label>}
      {media.length > 0 && <><fieldset><legend>How to publish images and text</legend><label className="publishing-radio"><input type="radio" name="media-mode" checked={mode === 'caption'} onChange={() => setMode('caption')} /> Images with the post as their caption</label><label className="publishing-radio"><input type="radio" name="media-mode" checked={mode === 'separate'} onChange={() => setMode('separate')} /> Images, then a separate text message</label></fieldset>{mode === 'separate' && <label>Image caption (optional)<textarea value={caption} onChange={e => setCaption(e.target.value)} rows={3} /></label>}<p className="publishing-note">Caption limit: {selected?.caption_limit || 1024} Telegram characters. Long text needs a separate message.</p></>}
      <p className="publishing-note">This schedules saved v{draft.current_version}. Later draft edits will not change it.</p>
      {error && <p className="publishing-error" role="alert">{error}</p>}
    </section><TelegramPreview value={{ body: draft.body, caption, mode, title: draft.working_title, media }} channel={selected?.title || selected?.identifier || ''} /></div>
    <footer className="publishing-dialog-footer"><span>Telegram publishes at the selected time.</span><button type="button" onClick={onClose} disabled={busy}>Cancel</button><button type="button" className="publishing-primary" onClick={() => void submit()} disabled={busy || !selected?.allowed || !date || (!draft.body.trim() && !media.length)}>{busy ? 'Scheduling…' : 'Confirm schedule'}</button></footer>
  </dialog>;
}
