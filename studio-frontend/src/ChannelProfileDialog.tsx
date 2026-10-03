import { useEffect, useRef, useState } from "react";
import { api, csrfToken, fetchStudioPost, type ChannelProfile, type StudioPost, StudioApiError } from "./api";
import { useDialogFocusTrap } from "./dialogFocus";

type Props = {
  channelId: number;
  onClose: () => void;
  onSaved?: (profile: ChannelProfile) => void;
};

export type ProfileTextState = { topics: string; editorial: string; style: string };
export type ProfileInitialState = ProfileTextState & { version: number };

export function isProfileDirty(
  initial: ProfileInitialState | null,
  current: ProfileTextState,
): boolean {
  if (!initial) return current.topics !== "" || current.editorial !== "" || current.style !== "";
  return (
    current.topics !== initial.topics ||
    current.editorial !== initial.editorial ||
    current.style !== initial.style
  );
}

export function needsBuildConfirm(topics: string, editorial: string, style: string): boolean {
  return Boolean(topics.trim() || editorial.trim() || style.trim());
}

export function isProfileConflictError(error: unknown): boolean {
  return error instanceof StudioApiError && error.status === 409;
}

export function extractConflictProfile(error: unknown): ChannelProfile | null {
  if (!(error instanceof StudioApiError)) return null;
  const server = (error.payload as unknown as { server_profile?: ChannelProfile }).server_profile;
  return server ?? null;
}

/** Supporting posts come from the saved analysis or current build and use
 * the authorized, full Telegram post reader. */
export function evidenceIdsFrom(profile: ChannelProfile | null): number[] {
  const ids = profile?.evidence_post_ids;
  if (!Array.isArray(ids)) return [];
  return ids.filter((id): id is number => Number.isInteger(id) && (id as number) > 0);
}

export function ProfileEvidence({ channelId, evidencePostIds }: { channelId: number; evidencePostIds: number[] }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  // Modal isolation: Tab cycles inside the evidence reader.
  useDialogFocusTrap(dialogRef);
  const [detail, setDetail] = useState<StudioPost | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  if (evidencePostIds.length === 0) return null;

  const openEvidence = async (postId: number) => {
    setLoading(true);
    setError("");
    setDetail(null);
    try {
      const post = await fetchStudioPost(postId);
      if (post.channel_id !== channelId) {
        setError("That post belongs to another channel, so it is not shown here.");
        return;
      }
      setDetail(post);
      dialogRef.current?.showModal();
    } catch {
      setError("Could not read that supporting post. Try again or open it from Post Explorer.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <section className="studio-profile-evidence" aria-label="Supporting posts">
      <h3>Supporting posts</h3>
      <p>Successful channel posts behind this profile. Each opens the full post — never an excerpt.</p>
      <ul>
        {evidencePostIds.map((postId) => (
          <li key={postId}>
            <button type="button" onClick={() => void openEvidence(postId)} disabled={loading}>
              Post #{postId}
            </button>
          </li>
        ))}
      </ul>
      {error && <p className="studio-profile-status is-error" role="alert">{error}</p>}
      <dialog ref={dialogRef} className="studio-settings studio-profile-evidence-reader" aria-label="Supporting post">
        <header>
          <h4>Post {detail ? `#${detail.message_id}` : ""}</h4>
          <button type="button" aria-label="Close supporting post" onClick={() => dialogRef.current?.close()}>Close</button>
        </header>
        {detail && (
          <>
            {/* formatted_html is server-sanitized Telegram HTML (T51 read model). */}
            <div className="studio-markdown" dangerouslySetInnerHTML={{ __html: detail.formatted_html || "" }} />
            <p className="studio-profile-meta">
              {detail.metrics.views.toLocaleString()} views · {detail.metrics.reactions.toLocaleString()} reactions · {detail.metrics.comments.toLocaleString()} comments · {detail.metrics.shares.toLocaleString()} shares
            </p>
            {detail.source_url && <p><a href={detail.source_url} target="_blank" rel="noopener noreferrer">Open original in Telegram</a></p>}
          </>
        )}
      </dialog>
    </section>
  );
}

function formatMeta(profile: ChannelProfile | null): string {
  if (!profile || (!profile.topics_text && !profile.editorial_text && !profile.style_text)) {
    return "Not built yet. Build the guidelines from your posts or write them yourself. Rules, not a template.";
  }
  const version = profile.version;
  const built = profile.built_from_posts ? ` · built from ${profile.built_from_posts} posts` : "";
  let saved = "";
  if (profile.updated_at) {
    try {
      const d = new Date(profile.updated_at);
      const day = d.getDate();
      const month = d.toLocaleString("en-GB", { month: "short" });
      const hours = String(d.getHours()).padStart(2, "0");
      const mins = String(d.getMinutes()).padStart(2, "0");
      saved = ` · saved ${day} ${month} ${hours}:${mins}`;
    } catch {
      saved = "";
    }
  } else if (profile.built_at) {
    try {
      const d = new Date(profile.built_at);
      const day = d.getDate();
      const month = d.toLocaleString("en-GB", { month: "short" });
      const hours = String(d.getHours()).padStart(2, "0");
      const mins = String(d.getMinutes()).padStart(2, "0");
      saved = ` · saved ${day} ${month} ${hours}:${mins}`;
    } catch {
      saved = "";
    }
  }
  return `v${version}${saved}${built}. The agent receives this text with every message as the channel’s guidelines. Rules, not a template.`;
}

function renderInlineMarkdown(line: string, key: number) {
  // Dialect: **bold**, *italic*, ~~strike~~, `code`, [text](https://url), > quote. No headings/images/HTML.
  // Strip images ![alt](url) -> nothing
  let text = line.replace(/!\[([^\]]*)\]\([^)]*\)/g, "");
  // Strip raw HTML
  text = text.replace(/<[a-zA-Z/][^>]*>/g, "");
  const parts: React.ReactNode[] = [];
  let lastIndex = 0;
  // Combined regex for bold, italic, strike, code, link
  const pattern = /(\*\*[^*]+\*\*|\*[^*]+\*|~~[^~]+~~|`[^`]+`|\[([^\]]+)\]\((https?:\/\/[^)]+)\)|\[([^\]]+)\]\([^)]+\))/g;
  let match: RegExpExecArray | null;
  let idx = 0;
  while ((match = pattern.exec(text)) !== null) {
    const start = match.index;
    if (start > lastIndex) {
      parts.push(<span key={`t-${key}-${idx++}`}>{text.slice(lastIndex, start)}</span>);
    }
    const token = match[0];
    if (token.startsWith("**")) {
      parts.push(<strong key={`b-${key}-${idx++}`}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith("~~")) {
      parts.push(<s key={`s-${key}-${idx++}`}>{token.slice(2, -2)}</s>);
    } else if (token.startsWith("`")) {
      parts.push(<code key={`c-${key}-${idx++}`}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith("[") && match[3]) {
      // http(s) link
      const label = match[2];
      const url = match[3];
      parts.push(
        <a key={`a-${key}-${idx++}`} href={url} target="_blank" rel="noopener noreferrer">
          {label}
        </a>,
      );
    } else if (token.startsWith("[") && match[4]) {
      // non-http link -> plain text
      parts.push(<span key={`l-${key}-${idx++}`}>{match[4]}</span>);
    } else if (token.startsWith("*") && !token.startsWith("**")) {
      parts.push(<em key={`i-${key}-${idx++}`}>{token.slice(1, -1)}</em>);
    } else {
      parts.push(<span key={`u-${key}-${idx++}`}>{token}</span>);
    }
    lastIndex = pattern.lastIndex;
  }
  if (lastIndex < text.length) {
    parts.push(<span key={`t-${key}-${idx}`}>{text.slice(lastIndex)}</span>);
  }
  if (parts.length === 0) return <span key={key}>{text}</span>;
  return <span key={key}>{parts}</span>;
}

export function PreviewMarkdown({ text }: { text: string }) {
  if (!text.trim()) return <div />;
  const lines = text.split("\n");
  return (
    <div>
      {lines.map((line, i) => line.trimStart().startsWith("> ")
        ? <blockquote key={i}>{renderInlineMarkdown(line.trimStart().slice(2), i)}</blockquote>
        : <p key={i}>{renderInlineMarkdown(line, i)}</p>)}
    </div>
  );
}

export default function ChannelProfileDialog({ channelId, onClose, onSaved }: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  // Modal isolation: Tab cycles inside the profile dialog.
  useDialogFocusTrap(dialogRef);
  // The profile opens formatted; raw-source editing is an explicit Edit
  // action, never the default preview.
  const [mode, setMode] = useState<"edit" | "preview">("preview");
  const [topics, setTopics] = useState("");
  const [editorial, setEditorial] = useState("");
  const [style, setStyle] = useState("");
  const [initial, setInitial] = useState<{ topics: string; editorial: string; style: string; version: number } | null>(null);
  const [profile, setProfile] = useState<ChannelProfile | null>(null);
  const [buildEvidence, setBuildEvidence] = useState<number[] | null>(null);
  const [canBuild, setCanBuild] = useState(true);
  const [blockers, setBlockers] = useState<Array<{ code: string; message: string }>>([]);
  const [building, setBuilding] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<string>("Build replaces the text in all three fields with a fresh analysis of your posts. Nothing is saved until you press Save.");
  const [statusIsError, setStatusIsError] = useState(false);
  const [conflictProfile, setConflictProfile] = useState<ChannelProfile | null>(null);

  const dirty = isProfileDirty(initial, { topics, editorial, style });
  const overLimit = topics.length > 2000 || editorial.length > 2000 || style.length > 2000;

  useEffect(() => {
    dialogRef.current?.showModal();
    let alive = true;
    // A/B isolation: the in-flight load is aborted on channel switch and a
    // delayed response for another channel never enters this dialog.
    const controller = new AbortController();
    void api<{ profile: ChannelProfile | null; can_build: boolean; build_blockers: Array<{ code: string; message: string }>; channel_id: number }>(`/studio/api/profile?channel_id=${encodeURIComponent(String(channelId))}`, { signal: controller.signal })
      .then((payload) => {
        if (!alive || payload.channel_id !== channelId) return;
        const p = payload.profile;
        setProfile(p);
        setBuildEvidence(null);
        setCanBuild(payload.can_build);
        setBlockers(payload.build_blockers);
        const t = p?.topics_text ?? "";
        const e = p?.editorial_text ?? "";
        const s = p?.style_text ?? "";
        setTopics(t);
        setEditorial(e);
        setStyle(s);
        setInitial({ topics: t, editorial: e, style: s, version: p?.version ?? 0 });
        if (!p || (!t && !e && !s)) {
          setStatus("Build replaces the text in all three fields with a fresh analysis of your posts. Nothing is saved until you press Save.");
          setStatusIsError(false);
        } else {
          setStatus("");
          setStatusIsError(false);
        }
      })
      .catch((error: unknown) => {
        if (!alive || (error instanceof DOMException && error.name === "AbortError")) return;
        setStatus("Could not load profile. Close and try again.");
        setStatusIsError(true);
      });
    return () => { alive = false; controller.abort(); };
  }, [channelId]);

  const closeWithConfirm = () => {
    if (dirty) {
      if (!window.confirm("Discard unsaved profile changes?")) return;
    }
    onClose();
  };

  const handleBuild = async () => {
    if (!canBuild) return;
    if (needsBuildConfirm(topics, editorial, style) && !window.confirm("Replace the text in all three fields with a fresh analysis? Nothing is saved until you press Save.")) {
      return;
    }
    setBuilding(true);
    setStatus("Analyzing posts. Compatibility retries can take up to 90 seconds.");
    setStatusIsError(false);
    try {
      const result = await api<{ draft: { topics_text: string; editorial_text: string; style_text: string; built_from_posts: number; evidence_post_ids?: number[]; limitations?: string[] } }>("/studio/api/profile/build", {
        method: "POST",
        headers: { "content-type": "application/json", "x-csrf-token": csrfToken() },
        body: JSON.stringify({ channel_id: channelId }),
      });
      const d = result.draft;
      setBuildEvidence(d.evidence_post_ids ?? []);
      setTopics(d.topics_text ?? "");
      setEditorial(d.editorial_text ?? "");
      setStyle(d.style_text ?? "");
      // A fresh build opens formatted; edits stay an explicit action.
      setMode("preview");
      setStatus("Build replaces the text in all three fields with a fresh analysis of your posts. Nothing is saved until you press Save.");
      setStatusIsError(false);
    } catch (e) {
      const message = e instanceof StudioApiError ? (e.payload.error?.message ?? "Could not build.") : "Could not build.";
      // Map blocker messages
      if (e instanceof StudioApiError && e.payload.error?.code === "too_few_posts") {
        setStatus(e.payload.error?.message ?? message);
      } else if (e instanceof StudioApiError && e.status === 409) {
        setStatus(message);
      } else {
        setStatus("Could not build. Your text is unchanged; try again.");
      }
      setStatusIsError(true);
    } finally {
      setBuilding(false);
    }
  };

  const handleSave = async (retryVersion?: number) => {
    const expected = retryVersion !== undefined ? retryVersion : (initial?.version ?? profile?.version ?? 0);
    setSaving(true);
    setStatus("");
    setStatusIsError(false);
    try {
      const result = await api<{ profile: ChannelProfile }>("/studio/api/profile", {
        method: "PUT",
        headers: { "content-type": "application/json", "x-csrf-token": csrfToken() },
        body: JSON.stringify({
          channel_id: channelId,
          expected_version: expected,
          ...(buildEvidence !== null ? { evidence_post_ids: buildEvidence } : {}),
          topics_text: topics,
          editorial_text: editorial,
          style_text: style,
        }),
      });
      const p = result.profile;
      setProfile(p);
      setBuildEvidence(null);
      setInitial({ topics: p.topics_text ?? "", editorial: p.editorial_text ?? "", style: p.style_text ?? "", version: p.version });
      setConflictProfile(null);
      setStatus(`Saved as v${p.version}. The agent uses it from the next message.`);
      setStatusIsError(false);
      onSaved?.(p);
    } catch (e) {
      if (isProfileConflictError(e) && e instanceof StudioApiError && e.payload) {
        const server = extractConflictProfile(e);
        if (server) {
          setConflictProfile(server);
          setStatusIsError(true);
          setStatus(""); // status will be rendered as conflict block
        } else {
          setStatus("Could not save. Your text is preserved; try again.");
          setStatusIsError(true);
        }
      } else {
        setStatus("Could not save. Your text is preserved; try again.");
        setStatusIsError(true);
      }
    } finally {
      setSaving(false);
    }
  };

  const loadTheirVersion = () => {
    if (!conflictProfile) return;
    setTopics(conflictProfile.topics_text ?? "");
    setEditorial(conflictProfile.editorial_text ?? "");
    setStyle(conflictProfile.style_text ?? "");
    setInitial({ topics: conflictProfile.topics_text ?? "", editorial: conflictProfile.editorial_text ?? "", style: conflictProfile.style_text ?? "", version: conflictProfile.version });
    setProfile(conflictProfile);
    setConflictProfile(null);
    setStatus("");
    setStatusIsError(false);
  };

  const saveMineAsNext = () => {
    if (!conflictProfile) return;
    void handleSave(conflictProfile.version);
  };

  const blockerMessage = !canBuild && blockers.length > 0 ? blockers[0].message : "";

  // Counter helper
  const counterClass = (value: string) => `studio-profile-counter${value.length > 2000 ? " is-over" : ""}`;

  return (
    <dialog
      className="studio-settings studio-profile"
      ref={dialogRef}
      aria-labelledby="studio-profile-title"
      onCancel={(e) => { e.preventDefault(); closeWithConfirm(); }}
      onClose={onClose}
    >
      <header>
        <h2 id="studio-profile-title">Channel profile</h2>
        <div className="studio-profile-header-actions">
          <button type="button" className="studio-profile-mode" aria-pressed={mode === "edit"} onClick={() => setMode("edit")} disabled={building}>
            Edit
          </button>
          <button type="button" className="studio-profile-mode" aria-pressed={mode === "preview"} onClick={() => setMode("preview")} disabled={building}>
            Preview
          </button>
          <button type="button" aria-label="Close channel profile" onClick={closeWithConfirm}>
            Close
          </button>
        </div>
      </header>
      <p className="studio-profile-meta">{formatMeta(profile)}</p>
      <ProfileEvidence channelId={channelId} evidencePostIds={buildEvidence ?? evidenceIdsFrom(profile)} />

      {mode === "edit" ? (
        <>
          <div className="studio-profile-field">
            <label htmlFor="studio-profile-topics">Topics</label>
            <p>What the channel writes about. One per line.</p>
            <textarea
              id="studio-profile-topics"
              maxLength={2000}
              placeholder="Budget and spending decisions — votes, tenders, audit findings"
              value={topics}
              onChange={(e) => setTopics(e.target.value)}
              disabled={building}
            />
            <div className={counterClass(topics)}>{topics.length.toLocaleString()} / 2,000</div>
          </div>

          <div className="studio-profile-field">
            <label htmlFor="studio-profile-editorial">Editorial rules</label>
            <p>What must and must not appear. One per line.</p>
            <textarea
              id="studio-profile-editorial"
              maxLength={2000}
              placeholder="Every factual claim about a third party carries a source link."
              value={editorial}
              onChange={(e) => setEditorial(e.target.value)}
              disabled={building}
            />
            <div className={counterClass(editorial)}>{editorial.length.toLocaleString()} / 2,000</div>
          </div>

          <div className="studio-profile-field">
            <label htmlFor="studio-profile-style">Style rules</label>
            <p>
              Tone, length, language, formatting habits. One per line. Markdown is supported and shows formatting: <code>**bold**</code> <code>*italic*</code> <code>~~strike~~</code> <code>`code`</code> <code>[text](https://url)</code> <code>&gt; quote</code>.
            </p>
            <textarea
              id="studio-profile-style"
              maxLength={2000}
              placeholder="The first line is the title, in bold: **Example title**"
              value={style}
              onChange={(e) => setStyle(e.target.value)}
              disabled={building}
            />
            <div className={counterClass(style)}>{style.length.toLocaleString()} / 2,000</div>
          </div>
        </>
      ) : (
        <>
          <div className="studio-profile-field">
            <label id="studio-profile-topics-preview-label">Topics</label>
            <p>What the channel writes about. One per line.</p>
            <div className="studio-profile-preview" role="region" aria-labelledby="studio-profile-topics-preview-label">
              <div className="studio-markdown">
                <PreviewMarkdown text={topics} />
              </div>
            </div>
          </div>

          <div className="studio-profile-field">
            <label id="studio-profile-editorial-preview-label">Editorial rules</label>
            <p>What must and must not appear. One per line.</p>
            <div className="studio-profile-preview" role="region" aria-labelledby="studio-profile-editorial-preview-label">
              <div className="studio-markdown">
                <PreviewMarkdown text={editorial} />
              </div>
            </div>
          </div>

          <div className="studio-profile-field">
            <label id="studio-profile-style-preview-label">Style rules</label>
            <p>
              Tone, length, language, formatting habits. One per line. Markdown is supported and shows formatting: <code>**bold**</code> <code>*italic*</code> <code>~~strike~~</code> <code>`code`</code> <code>[text](https://url)</code> <code>&gt; quote</code>.
            </p>
            <div className="studio-profile-preview" role="region" aria-labelledby="studio-profile-style-preview-label">
              <div className="studio-markdown">
                <PreviewMarkdown text={style} />
              </div>
            </div>
          </div>
        </>
      )}

      <footer className="studio-profile-actions">
        <button
          type="button"
          onClick={() => void handleBuild()}
          disabled={building || !canBuild}
          title={!canBuild ? blockerMessage : undefined}
        >
          {building ? "Analyzing posts…" : "Build from posts"}
        </button>
        <span>{dirty ? "Unsaved changes" : ""}</span>
        <button
          type="button"
          className="studio-copy"
          onClick={() => void handleSave()}
          disabled={!dirty || saving || building || overLimit}
        >
          {dirty ? `Save as v${(profile?.version ?? initial?.version ?? 0) + 1}` : "Save"}
        </button>
      </footer>
      {overLimit && !building ? (
        <p className="studio-profile-status is-error" role="alert">
          One field exceeds 2,000 characters. Shorten it before saving.
        </p>
      ) : conflictProfile ? (
        <p className="studio-profile-status is-error" role="alert">
          Someone saved v{conflictProfile.version} while you were editing. <button type="button" onClick={loadTheirVersion}>Load their version</button>
          <button type="button" onClick={saveMineAsNext}>Save mine as v{conflictProfile.version + 1}</button>
        </p>
      ) : status ? (
        <p className={`studio-profile-status${statusIsError ? " is-error" : ""}`} role={statusIsError ? "alert" : "status"}>
          {status}
          {!canBuild && blockerMessage && !building && !statusIsError ? ` ${blockerMessage}` : ""}
        </p>
      ) : blockerMessage && !canBuild ? (
        <p className="studio-profile-status" role="status">
          {blockerMessage}
        </p>
      ) : mode === "preview" ? (
        <p className="studio-profile-status" role="status">
          Preview shows how the agent reads the formatting. Switch to Edit to change the text.
        </p>
      ) : null}
    </dialog>
  );
}
