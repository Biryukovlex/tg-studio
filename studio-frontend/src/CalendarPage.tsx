import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { api, csrfToken, type Draft } from './api';
import { useDialogFocusTrap } from './dialogFocus';
import { ScheduleDialog, TelegramPreview, STATUS_LABELS, timezonePreference, zonedInput, zoneOptions, type ScheduledPost, type PublishingChannel } from './publishing';

type Mode = 'month' | 'week' | 'list';
export function addDays(day: string, amount: number) { const d = new Date(`${day}T12:00:00Z`); d.setUTCDate(d.getUTCDate() + amount); return d.toISOString().slice(0, 10); }
export function calendarDays(anchor: string, mode: Mode) {
  const first = mode === 'month' ? `${anchor.slice(0, 7)}-01` : anchor;
  const weekday = new Date(`${first}T12:00:00Z`).getUTCDay();
  const start = addDays(first, -((weekday + 6) % 7));
  return Array.from({ length: mode === 'week' ? 7 : 42 }, (_, i) => addDays(start, i));
}
function channelColor(id: number) { return ['#68dfd5', '#7eafff', '#f2b86b', '#bb9bf4', '#ee96aa', '#81cda2'][Math.abs(id) % 6]; }
function dayLabel(day: string, options: Intl.DateTimeFormatOptions) { return new Intl.DateTimeFormat(undefined, { ...options, timeZone: 'UTC' }).format(new Date(`${day}T12:00:00Z`)); }
function ScheduleCard({ post, zone, onOpen }: { post: ScheduledPost; zone: string; onOpen: (post: ScheduledPost) => void }) {
  const color = channelColor(post.channel_id);
  return <button type="button" className={`calendar-card status-${post.status}`} style={{ '--channel-color': color } as CSSProperties} onClick={() => onOpen(post)} aria-label={`${STATUS_LABELS[post.status]}: ${post.channel_title}, ${zonedInput(new Date(post.scheduled_at), zone)}`}>
    <div className="calendar-card-meta"><time>{zonedInput(new Date(post.scheduled_at), zone).slice(11)}</time><span className={`publishing-status status-${post.status}`}>{STATUS_LABELS[post.status]}</span></div>
    <strong>{post.channel_title || post.channel_identifier}</strong><div className="calendar-card-content">{post.snapshot.media[0] && <img src={post.snapshot.media[0].url} alt="" />}<span>{post.snapshot.title || post.snapshot.body || 'Photo post'}</span></div>{post.snapshot.media.length > 1 && <span className="calendar-image-count">{post.snapshot.media.length} images</span>}
  </button>;
}

function PostDialog({ post, onClose, onChanged }: { post: ScheduledPost; onClose: () => void; onChanged: () => void }) {
  const ref = useRef<HTMLDialogElement>(null); useDialogFocusTrap(ref);
  const [current, setCurrent] = useState(post);
  const [editing, setEditing] = useState(false);
  const [zone, setZone] = useState(post.timezone);
  const [date, setDate] = useState(zonedInput(new Date(post.scheduled_at), post.timezone));
  const [body, setBody] = useState(post.snapshot.body);
  const [caption, setCaption] = useState(post.snapshot.caption);
  const [fold, setFold] = useState('');
  const [events, setEvents] = useState<{ id: number; status: string; message: string; created_at: string }[]>([]);
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false); const [confirmCancel, setConfirmCancel] = useState(false);
  useEffect(() => { ref.current?.showModal(); }, []);
  useEffect(() => { let active = true; const load = () => api<{ post: ScheduledPost; events: typeof events }>(`/studio/api/publishing/posts/${post.id}`).then(r => { if (active) { setEvents(r.events); if (!editing) setCurrent(r.post); } }).catch(e => { if (active) setError(e.message); }); void load(); const id = window.setInterval(() => { void load(); }, 5000); return () => { active = false; clearInterval(id); }; }, [post.id, editing]);
  const mutate = async (cancel: boolean) => {
    setBusy(true); setError('');
    try { const result = await api<{ post: ScheduledPost }>(`/studio/api/publishing/posts/${post.id}${cancel ? '/cancel' : ''}`, { method: cancel ? 'POST' : 'PATCH', headers: { 'content-type': 'application/json', 'x-csrf-token': csrfToken() }, body: JSON.stringify(cancel ? { expected_revision: current.revision, confirm: true } : { expected_revision: current.revision, confirm: true, local_datetime: date, timezone: zone, fold: fold === '' ? null : Number(fold), body, caption }) }); setCurrent(result.post); setEditing(false); setConfirmCancel(false); onChanged(); }
    catch (e) { setError(e instanceof Error ? e.message : 'Operation failed.'); } finally { setBusy(false); }
  };
  const [resolving, setResolving] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const resolveReview = async () => {
    if (!reviewed || busy) return;
    setBusy(true); setError('');
    try { const result = await api<{ post: ScheduledPost }>(`/studio/api/publishing/posts/${post.id}/resolve`, { method: 'POST', headers: { 'content-type': 'application/json', 'x-csrf-token': csrfToken() }, body: JSON.stringify({ expected_revision: current.revision, confirm: true, confirmed_removed_in_telegram: true }) }); setCurrent(result.post); setResolving(false); onChanged(); }
    catch (e) { setError(e instanceof Error ? e.message : 'Could not record the resolution.'); } finally { setBusy(false); }
  };
  const published = current.parts.find(p => p.published_id);
  const telegramUrl = published ? (current.channel_identifier.startsWith('@') ? `https://t.me/${current.channel_identifier.slice(1)}/${published.published_id}` : current.channel_chat_id ? `https://t.me/c/${String(current.channel_chat_id).replace(/^-100/, '').replace(/^-/, '')}/${published.published_id}` : '') : '';
  const canEdit = ['scheduled', 'failed'].includes(current.status);
  const canCancel = current.status === 'queued' || ['scheduled', 'needs_review'].includes(current.status) && current.parts.length > 0 && current.parts.every(p => p.scheduled_id && !p.published_id);
  return <dialog ref={ref} className="publishing-dialog" aria-labelledby="calendar-post-title" onCancel={e => { if (e.target !== e.currentTarget) return; e.preventDefault(); if (!busy) onClose(); }}>
    <header className="publishing-dialog-header"><div><p className="publishing-eyebrow">{current.channel_title || current.channel_identifier}</p><h2 id="calendar-post-title">{editing ? 'Edit scheduled post' : 'Scheduled post'}</h2></div><button type="button" className="publishing-icon" aria-label="Close post" disabled={busy} onClick={onClose}>×</button></header>
    <div className="publishing-dialog-body"><section className="schedule-fields"><span className={`publishing-status status-${current.status}`}>{STATUS_LABELS[current.status]}</span><p className="calendar-detail-date">{zonedInput(new Date(current.scheduled_at), current.timezone).replace('T', ' · ')}<br /><span>{current.timezone}</span></p>{current.error && <p className="publishing-error" role="alert">{current.error}</p>}
      {resolving && <div className="publishing-error"><p>Open this channel’s scheduled messages in Telegram and remove every remaining message belonging to this post. This action only closes the local record; it cannot undo earlier publication.</p><label className="publishing-radio"><input type="checkbox" checked={reviewed} onChange={e => setReviewed(e.target.checked)} /> I checked Telegram and removed all remaining scheduled messages.</label></div>}{editing && <><label>Date and time<input type="datetime-local" value={date} onChange={e => setDate(e.target.value)} /></label><label>Timezone<select value={zone} onChange={e => { setZone(e.target.value); setFold(''); }}>{zoneOptions(zone).map(z => <option key={z}>{z}</option>)}</select></label><label>Post text<textarea rows={8} value={body} onChange={e => setBody(e.target.value)} /></label>{current.snapshot.mode === 'separate' && current.snapshot.media.length > 0 && <label>Image caption<textarea rows={3} value={caption} onChange={e => setCaption(e.target.value)} /></label>}{error.includes('occurs twice') && <label>Time occurrence<select value={fold} onChange={e => setFold(e.target.value)}><option value="">Choose</option><option value="0">First occurrence</option><option value="1">Second occurrence</option></select></label>}<p className="publishing-note">This edits the Telegram schedule. The original Studio draft stays unchanged.</p></>}
      {!editing && current.conversation_id && <a className="publishing-link" href={`/studio?conversation_id=${current.conversation_id}`}>Open original draft in Studio ↗</a>}{telegramUrl && <a className="publishing-link" href={telegramUrl} target="_blank" rel="noopener noreferrer">View published post in Telegram ↗</a>}
      <details className="publishing-history"><summary>Activity</summary><ol>{events.map(e => <li key={e.id}><strong>{STATUS_LABELS[e.status] || e.status}</strong><time>{zonedInput(new Date(e.created_at), zone).replace('T', ' · ')}</time><p>{e.message}</p></li>)}</ol></details>
      {error && <p className="publishing-error" role="alert">{error}</p>}
    </section><TelegramPreview channel={current.channel_title || current.channel_identifier} value={editing ? { ...current.snapshot, body, caption, external_body_html: undefined } : current.snapshot} /></div>
    <footer className="publishing-dialog-footer">{resolving ? <><button type="button" disabled={busy} onClick={() => setResolving(false)}>Back</button><button type="button" className="publishing-danger" disabled={busy || !reviewed} onClick={() => void resolveReview()}>Confirm manual resolution</button></> : confirmCancel ? <><span>Cancel every message in this scheduled post?</span><button type="button" disabled={busy} onClick={() => setConfirmCancel(false)}>Keep post</button><button type="button" className="publishing-danger" disabled={busy} onClick={() => void mutate(true)}>{busy ? 'Cancelling…' : 'Confirm cancellation'}</button></> : editing ? <><button type="button" disabled={busy} onClick={() => setEditing(false)}>Discard edits</button><button type="button" className="publishing-primary" disabled={busy} onClick={() => void mutate(false)}>{busy ? 'Updating…' : 'Confirm changes'}</button></> : <><span>{current.snapshot.media.length ? `${current.snapshot.media.length} images · ` : ''}{current.snapshot.mode === 'separate' && current.snapshot.media.length ? 'Images + text message' : 'One post'}</span>{current.status === 'needs_review' && <button type="button" onClick={() => { setReviewed(false); setResolving(true); }}>Resolve after checking Telegram</button>}{canCancel && <button type="button" className="publishing-danger" onClick={() => setConfirmCancel(true)}>Cancel post</button>}{canEdit && <button type="button" className="publishing-primary" onClick={() => { setDate(zonedInput(new Date(current.scheduled_at), current.timezone)); setZone(current.timezone); setBody(current.snapshot.body); setCaption(current.snapshot.caption); setEditing(true); }}>Edit / reschedule</button>}</>}</footer>
  </dialog>;
}

function DraftPicker({ date, onClose, onChoose }: { date: string; onClose: () => void; onChoose: (draft: Draft, date: string) => void }) {
  const ref = useRef<HTMLDialogElement>(null); useDialogFocusTrap(ref);
  const [drafts, setDrafts] = useState<{ id: string; working_title: string; body: string; channel_title: string }[]>([]);
  const [search, setSearch] = useState(''); const [error, setError] = useState(''); const [loading, setLoading] = useState(true);
  useEffect(() => { ref.current?.showModal(); void api<{ drafts: typeof drafts }>('/studio/api/publishing/drafts').then(r => setDrafts(r.drafts)).catch(e => setError(e.message)).finally(() => setLoading(false)); }, []);
  const choose = async (id: string) => { try { const r = await api<{ draft: Draft }>(`/studio/api/drafts/${id}`); onChoose(r.draft, date); } catch (e) { setError(e instanceof Error ? e.message : 'Could not load draft.'); } };
  return <dialog ref={ref} className="publishing-dialog draft-picker" aria-labelledby="draft-picker-title" onCancel={e => { e.preventDefault(); onClose(); }}><header className="publishing-dialog-header"><div><p className="publishing-eyebrow">Schedule an existing draft</p><h2 id="draft-picker-title">Choose a post</h2></div><button type="button" className="publishing-icon" aria-label="Close draft picker" onClick={onClose}>×</button></header><div className="draft-picker-body"><label>Search drafts<input type="search" value={search} onChange={e => setSearch(e.target.value)} placeholder="Title, channel or text" /></label>{error && <p className="publishing-error" role="alert">{error}</p>}{loading ? <p role="status">Loading drafts…</p> : drafts.length ? <div className="draft-picker-list">{drafts.filter(d => `${d.working_title} ${d.body} ${d.channel_title}`.toLowerCase().includes(search.toLowerCase())).map(d => <button type="button" key={d.id} onClick={() => void choose(d.id)}><span>{d.channel_title}</span><strong>{d.working_title || d.body.slice(0, 80)}</strong><p>{d.body.slice(0, 130)}</p></button>)}</div> : <div className="calendar-empty"><h3>No drafts yet</h3><p>Create a draft in Studio, then come back to schedule it.</p><a href="/studio" className="publishing-link">Open Studio ↗</a></div>}</div></dialog>;
}

export function CalendarPage() {
  const [zone, setZone] = useState(timezonePreference);
  const [anchor, setAnchor] = useState(() => zonedInput(new Date(), timezonePreference()).slice(0, 10));
  const [mode, setMode] = useState<Mode>(() => window.innerWidth < 760 ? 'list' : 'month');
  const [posts, setPosts] = useState<ScheduledPost[]>([]); const [channels, setChannels] = useState<PublishingChannel[]>([]);
  const [channel, setChannel] = useState(''); const [status, setStatus] = useState(''); const [search, setSearch] = useState('');
  const [error, setError] = useState(''); const [truncated, setTruncated] = useState(false); const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<ScheduledPost | null>(null); const [picking, setPicking] = useState<string | null>(null);
  const [schedule, setSchedule] = useState<{ draft: Draft; date: string } | null>(null); const [refresh, setRefresh] = useState(0);
  const weekRef = useRef<HTMLElement>(null); const weekScope = useRef('');
  const [notice, setNotice] = useState(''); const [focusDay, setFocusDay] = useState<string | null>(null);
  const days = useMemo(() => calendarDays(anchor, mode), [anchor, mode]);
  const today = zonedInput(new Date(), zone).slice(0, 10);
  useEffect(() => { try { localStorage.setItem('calendar-timezone', zone); } catch { /* Optional preference. */ } }, [zone]);
  useEffect(() => {
    let active = true; const controller = new AbortController();
    const load = async () => {
      try {
        const start = `${addDays(days[0], -1)}T00:00:00Z`, end = `${addDays(days[days.length - 1], 2)}T00:00:00Z`;
        const query = new URLSearchParams({ start, end, search }); if (channel) query.set('channel_id', channel); if (status) query.set('status', status);
        const [p, c] = await Promise.all([api<{ posts: ScheduledPost[]; truncated: boolean }>(`/studio/api/publishing/posts?${query}`, { signal: controller.signal }), api<{ channels: PublishingChannel[] }>('/studio/api/publishing/channels', { signal: controller.signal })]);
        if (active) { setPosts(p.posts); setChannels(c.channels); setTruncated(p.truncated); setError(''); setLoading(false); }
      } catch (e) { if (active && !controller.signal.aborted) { setError(e instanceof Error ? e.message : 'Could not load calendar.'); setLoading(false); } }
    };
    setLoading(true); const timeout = setTimeout(() => { void load(); }, 180); const interval = window.setInterval(() => { void load(); }, 5000);
    return () => { active = false; controller.abort(); clearTimeout(timeout); clearInterval(interval); };
  }, [days[0], days[days.length - 1], channel, status, search, refresh]);
  const grouped = useMemo(() => { const result = new Map<string, ScheduledPost[]>(); posts.forEach(p => { const day = zonedInput(new Date(p.scheduled_at), zone).slice(0, 10); const values = result.get(day) || []; values.push(p); result.set(day, values); }); return result; }, [posts, zone]);
  const listedDays = mode === 'list' && !focusDay ? days.filter(day => day.slice(0,7) === anchor.slice(0,7)) : days;
  useEffect(() => {
    if (mode !== 'week') { weekScope.current = ''; return; }
    const scope = `${anchor}:${zone}`; if (loading || weekScope.current === scope || !weekRef.current) return;
    const hours = posts.filter(p => days.includes(zonedInput(new Date(p.scheduled_at), zone).slice(0,10))).map(p => Number(zonedInput(new Date(p.scheduled_at), zone).slice(11,13)));
    const hour = hours.length ? Math.max(0, Math.min(...hours) - 1) : 9;
    const row = weekRef.current.querySelector(`[data-hour="${hour}"]`) as HTMLElement | null;
    if (row) weekRef.current.scrollTop = Math.max(0, row.offsetTop - 45);
    weekScope.current = scope;
  }, [mode, anchor, zone, loading, posts]);
  const visibleCount = listedDays.reduce((sum, day) => sum + (grouped.get(day)?.length || 0), 0);
  const period = mode === 'week' ? `${dayLabel(days[0], { month: 'short', day: 'numeric' })} – ${dayLabel(days[6], { month: 'short', day: 'numeric', year: 'numeric' })}` : dayLabel(`${anchor.slice(0, 7)}-01`, { month: 'long', year: 'numeric' });
  const navigate = (delta: number) => { setFocusDay(null); if (mode === 'week') setAnchor(addDays(anchor, delta * 7)); else { const d = new Date(`${anchor.slice(0, 7)}-01T12:00:00Z`); d.setUTCMonth(d.getUTCMonth() + delta); setAnchor(d.toISOString().slice(0, 10)); } };
  const newAt = (day: string, hour = '10:00') => setPicking(`${day}T${hour}`);
  return <div className="calendar-app"><div className="calendar-toolbar"><div className="calendar-period"><button type="button" onClick={() => { setAnchor(today); setFocusDay(null); }}>Today</button><button type="button" className="publishing-icon" aria-label="Previous period" onClick={() => navigate(-1)}>‹</button><h2>{period}</h2><button type="button" className="publishing-icon" aria-label="Next period" onClick={() => navigate(1)}>›</button></div><div className="calendar-modes" role="group" aria-label="Calendar view">{(['month', 'week', 'list'] as Mode[]).map(m => <button key={m} type="button" aria-pressed={mode === m} onClick={() => { setMode(m); setFocusDay(null); }}>{m[0].toUpperCase() + m.slice(1)}</button>)}</div><button type="button" className="publishing-primary calendar-new" onClick={() => setPicking(zonedInput(new Date(Date.now() + 3600_000), zone))}>+ Schedule post</button></div>
    <div className="calendar-filters"><label className="calendar-search"><span className="sr-only">Search posts</span><input type="search" value={search} onChange={e => setSearch(e.target.value)} placeholder="Search posts or channels" /></label><label><span className="sr-only">Channel</span><select value={channel} onChange={e => setChannel(e.target.value)}><option value="">All channels</option>{channels.map(c => <option key={c.id} value={c.id}>{c.title || c.identifier}</option>)}</select></label><label><span className="sr-only">Status</span><select value={status} onChange={e => setStatus(e.target.value)}><option value="">All statuses</option>{Object.entries(STATUS_LABELS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><label className="calendar-timezone"><span>Timezone</span><select aria-label="Calendar timezone" value={zone} onChange={e => setZone(e.target.value)}>{zoneOptions(zone).map(z => <option key={z}>{z}</option>)}</select></label></div>
    <div className="calendar-feedback" aria-live="polite">{error ? <span className="publishing-error">{error} <button type="button" onClick={() => setRefresh(r => r + 1)}>Retry</button></span> : notice || (loading ? 'Loading calendar…' : `${visibleCount} ${visibleCount === 1 ? 'post' : 'posts'} in this period`)}{truncated && <span>Showing the first 1000 results. Narrow your filters.</span>}</div>
    {mode === 'month' && <section className="calendar-month" aria-label="Monthly calendar"><div className="calendar-weekdays">{['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map(d => <span key={d}>{d}</span>)}</div><div className="calendar-month-grid">{days.map(day => <div key={day} className={`calendar-day ${day.slice(0, 7) !== anchor.slice(0, 7) ? 'other-month' : ''} ${day === today ? 'is-today' : ''}`} onClick={e => { if (e.target === e.currentTarget) newAt(day); }}><div className="calendar-day-heading"><button type="button" className="calendar-day-number" aria-label={`Schedule on ${day}`} onClick={() => newAt(day)}>{Number(day.slice(8))}</button><button type="button" className="calendar-day-add" aria-label={`Add post on ${day}`} onClick={() => newAt(day)}>+</button></div>{(grouped.get(day) || []).slice(0, 3).map(p => <ScheduleCard key={p.id} post={p} zone={zone} onOpen={setSelected} />)}{(grouped.get(day)?.length || 0) > 3 && <button type="button" className="calendar-more" onClick={() => { setFocusDay(day); setMode('list'); }}>+ {(grouped.get(day)?.length || 0) - 3} more</button>}</div>)}</div></section>}
    {mode === 'week' && <section ref={weekRef} className="calendar-week" aria-label="Weekly calendar"><div className="calendar-week-head"><span />{days.map(day => <button type="button" key={day} className={day === today ? 'is-today' : ''} onClick={() => newAt(day)}>{dayLabel(day, { weekday: 'short', day: 'numeric', month: 'short' })}</button>)}</div>{Array.from({ length: 24 }, (_, hour) => <div className="calendar-hour-row" key={hour} data-hour={hour}><time>{String(hour).padStart(2, '0')}:00</time>{days.map(day => <div className="calendar-hour-slot" key={day}><button type="button" className="calendar-slot-add" aria-label={`Schedule ${day} at ${hour}:00`} onClick={() => newAt(day, `${String(hour).padStart(2, '0')}:00`)}>+</button>{(grouped.get(day) || []).filter(p => Number(zonedInput(new Date(p.scheduled_at), zone).slice(11, 13)) === hour).map(p => <ScheduleCard key={p.id} post={p} zone={zone} onOpen={setSelected} />)}</div>)}</div>)}</section>}
    {mode === 'list' && <section className="calendar-agenda" aria-label="Scheduled posts list">{focusDay && <button type="button" className="publishing-link" onClick={() => setFocusDay(null)}>Show the whole period</button>}{listedDays.filter(day => (!focusDay || day === focusDay) && (grouped.get(day)?.length || 0) > 0).map(day => <section className="calendar-agenda-day" key={day}><h3>{dayLabel(day, { weekday: 'long', day: 'numeric', month: 'long' })}{day === today && <span>Today</span>}</h3><div>{(grouped.get(day) || []).map(p => <ScheduleCard key={p.id} post={p} zone={zone} onOpen={setSelected} />)}</div></section>)}{!loading && !visibleCount && <div className="calendar-empty"><div className="calendar-empty-icon" aria-hidden="true">▦</div><h3>{channel || status || search ? 'No matching posts' : 'Your next story starts here'}</h3><p>{channel || status || search ? 'Change the filters or choose another period.' : 'Schedule a draft from Studio. Its channel and publishing time will appear here.'}</p><button type="button" className="publishing-primary" onClick={() => newAt(today)}>Schedule a post</button></div>}</section>}
    <p className="calendar-scope-note">Schedules created in TGhost. Changes made in Telegram are checked automatically.</p>
    {selected && <PostDialog key={selected.id} post={selected} onClose={() => setSelected(null)} onChanged={() => setRefresh(r => r + 1)} />}
    {picking && <DraftPicker date={picking} onClose={() => setPicking(null)} onChoose={(draft, date) => { setPicking(null); setSchedule({ draft, date }); }} />}
    {schedule && <ScheduleDialog draft={schedule.draft} initialDate={schedule.date} onClose={() => setSchedule(null)} onScheduled={() => { setSchedule(null); setRefresh(r => r + 1); setNotice('Queued for Telegram. The status will change once Telegram confirms the schedule.'); }} />}
  </div>;
}
