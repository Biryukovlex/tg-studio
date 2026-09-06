import { useEffect, useRef, useState } from "react";
import { api, csrfToken, type ChannelProfile, StudioApiError } from "./api";

type Props = {
  channelId: number;
  onClose: () => void;
  onSaved?: (profile: ChannelProfile) => void;
};

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
  text = text.replace(/<[a-zA-Z\/][^>]*>/g, "");
  const parts: React.ReactNode[] = [];
  let lastIndex = 0;
  // Combined regex for bold, italic, strike, code, link
  const pattern = /(\*\*[^*]+\*\*|\*[^*]+\*|~~[^~]+~~|`[^`]+`|\[([^\]]+)\]\((https?:\/\/[^)]+)\)|\[([^\]]+)\]\([^)]+\)|> .+)/g;
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
    } else if (token.startsWith("> ")) {
      parts.push(<blockquote key={`q-${key}-${idx++}`}><span>{token.slice(2)}</span></blockquote>);
    } else {
      parts.push(<span key={`u-${key}-${idx++}`}>{token}</span>);
    }
    lastIndex = pattern.lastIndex;
  }
  if (lastIndex < text.length) {
    parts.push(<span key={`t-${key}-${idx++}`}>{text.slice(lastIndex)}</span>);
  }
  if (parts.length === 0) return <span key={key}>{line}</span>;
  return <span key={key}>{parts}</span>;
}

function PreviewMarkdown({ text }: { text: string }) {
  if (!text.trim()) return <p />;
  const lines = text.split("\n");
  return (
    <p>
      {lines.map((line, i) => (
        <span key={i}>
          {renderInlineMarkdown(line, i)}
          {i < lines.length - 1 && <br />}
        </span>
      ))}
    </p>
  );
}

export default function ChannelProfileDialog({ channelId, onClose, onSaved }: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [mode, setMode] = useState<"edit" | "preview">("edit");
  const [topics, setTopics] = useState("");
  const [editorial, setEditorial] = useState("");
  const [style, setStyle] = useState("");
  const [initial, setInitial] = useState<{ topics: string; editorial: string; style: string; version: number } | null>(null);
  const [profile, setProfile] = useState<ChannelProfile | null>(null);
  const [canBuild, setCanBuild] = useState(true);
  const [blockers, setBlockers] = useState<Array<{ code: string; message: string }>>([]);
  const [building, setBuilding] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<string>("Build replaces the text in all three fields with a fresh analysis of your posts. Nothing is saved until you press Save.");
  const [statusIsError, setStatusIsError] = useState(false);
  const [conflictProfile, setConflictProfile] = useState<ChannelProfile | null>(null);

  const dirty = initial ? (topics !== initial.topics || editorial !== initial.editorial || style !== initial.style) : (topics !== "" || editorial !== "" || style !== "");

  useEffect(() => {
    dialogRef.current?.showModal();
    let alive = true;
    void api<{ profile: ChannelProfile | null; can_build: boolean; build_blockers: Array<{ code: string; message: string }>; channel_id: number }>(`/studio/api/profile?channel_id=${encodeURIComponent(String(channelId))}`)
      .then((payload) => {
        if (!alive) return;
        const p = payload.profile;
        setProfile(p);
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
      .catch(() => {
        if (!alive) return;
        setStatus("Could not load profile. Close and try again.");
        setStatusIsError(true);
      });
    return () => { alive = false; };
  }, [channelId]);

  const closeWithConfirm = () => {
    if (dirty) {
      if (!window.confirm("Discard unsaved profile changes?")) return;
    }
    onClose();
  };

  const handleBuild = async () => {
    if (!canBuild) return;
    if ((topics.trim() || editorial.trim() || style.trim()) && !window.confirm("Replace the text in all three fields with a fresh analysis? Nothing is saved until you press Save.")) {
      return;
    }
    setBuilding(true);
    setStatus("Analyzing posts. This usually takes under a minute.");
    setStatusIsError(false);
    try {
      const result = await api<{ draft: { topics_text: string; editorial_text: string; style_text: string; built_from_posts: number } }>("/studio/api/profile/build", {
        method: "POST",
        headers: { "content-type": "application/json", "x-csrf-token": csrfToken() },
        body: JSON.stringify({ channel_id: channelId }),
      });
      const d = result.draft;
      setTopics(d.topics_text ?? "");
      setEditorial(d.editorial_text ?? "");
      setStyle(d.style_text ?? "");
      setMode("edit");
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
          topics_text: topics,
          editorial_text: editorial,
          style_text: style,
        }),
      });
      const p = result.profile;
      setProfile(p);
      setInitial({ topics: p.topics_text ?? "", editorial: p.editorial_text ?? "", style: p.style_text ?? "", version: p.version });
      setConflictProfile(null);
      setStatus(`Saved as v${p.version}. The agent uses it from the next message.`);
      setStatusIsError(false);
      onSaved?.(p);
    } catch (e) {
      if (e instanceof StudioApiError && e.status === 409 && e.payload) {
        const server = (e.payload as unknown as { server_profile: ChannelProfile }).server_profile;
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
          disabled={!dirty || saving || building || topics.length > 2000 || editorial.length > 2000 || style.length > 2000}
        >
          {dirty ? `Save as v${(profile?.version ?? initial?.version ?? 0) + 1}` : "Save"}
        </button>
      </footer>
      {conflictProfile ? (
        <p className="studio-profile-status is-error" role="alert">
          Someone saved v{conflictProfile.version} while you were editing. <button type="button" onClick={loadTheirVersion}>Load their version</button>
          <button type="button" onClick={saveMineAsNext}>Save mine as v{conflictProfile.version + 1}</button>
        </p>
      ) : status ? (
        <p className={`studio-profile-status${statusIsError ? " is-error" : ""}`} role={statusIsError ? "alert" : "status"}>
          {status}
          {!canBuild && blockerMessage && !building && !statusIsError ? ` ${blockerMessage}` : ""}
          {building ? " Analyzing posts. This usually takes under a minute." : ""}
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
