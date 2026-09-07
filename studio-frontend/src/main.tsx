import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  type ToolCallMessagePartProps,
  useAuiState,
} from "@assistant-ui/react";
import { MarkdownTextPrimitive } from "@assistant-ui/react-markdown";
import { HttpAgent } from "@ag-ui/client";
import { useAgUiRuntime } from "@assistant-ui/react-ag-ui";
import remarkGfm from "remark-gfm";
import {
  api,
  asThreadMessages,
  csrfToken,
  type Bootstrap,
  type Conversation,
  type Draft,
  type DraftVersion,
  type PersistedMessage,
  type RunDetails,
  type RunEvent,
  type RunSummary,
  type RunUsage,
  StudioApiError,
} from "./api";
import { isTerminalPollStatus, nextPollDelay, shouldStopPollingAfterErrors } from "./runPolling";
import { copyRenderedSelection, copyRichText, htmlFromMarkdown, plainFromMarkdown, telegramMarkupFromMarkdown } from "./markdownCopy";
import ChannelProfileDialog from "./ChannelProfileDialog";
import "./styles.css";

function ToolActivity({ toolName, result }: ToolCallMessagePartProps) {
  const label = toolActivityLabel(toolName);
  return (
    <div className="studio-tool" role="status">
      <span className="studio-tool-dot" aria-hidden="true" />
      {result === undefined ? `${label}…` : completedToolActivityLabel(toolName)}
    </div>
  );
}

function StudioMarkdownText() {
  return (
    <MarkdownTextPrimitive
      className="studio-markdown"
      defer
      remarkPlugins={[remarkGfm]}
      components={{
        img: () => null,
        a: ({ node: _node, ...props }) => (
          <a {...props} target="_blank" rel="noopener noreferrer" />
        ),
      }}
    />
  );
}

function StudioMessage() {
  const role = useAuiState((state) => state.message.role);

  return (
    <MessagePrimitive.Root className="studio-message" data-role={role}>
      <MessagePrimitive.If assistant>
        <MessagePrimitive.Parts
          components={{
            Text: StudioMarkdownText,
            tools: { Fallback: ToolActivity },
          }}
        />
      </MessagePrimitive.If>
      <MessagePrimitive.If user>
        <MessagePrimitive.Parts components={{ tools: { Fallback: ToolActivity } }} />
      </MessagePrimitive.If>
      <MessagePrimitive.If system>
        <MessagePrimitive.Parts components={{ tools: { Fallback: ToolActivity } }} />
      </MessagePrimitive.If>
    </MessagePrimitive.Root>
  );
}

const TOOL_ACTIVITY_LABELS: Record<string, string> = {
  get_channel_context: "Reading channel history",
  analyze_channel: "Analyzing post performance",
  get_topic_profile: "Reviewing channel topics",
  search_web: "Searching the web",
  read_sources: "Reading source pages",
  compare_sources: "Cross-checking sources",
  find_novel_topics: "Finding fresh story angles",
  propose_topic_changes: "Preparing a topic proposal",
  apply_confirmed_topic_changes: "Updating the channel profile",
  explain_recommendation: "Connecting the evidence",
  get_draft: "Reading the current draft",
  create_draft: "Building the draft artifact",
  revise_draft: "Revising the draft artifact",
  save_draft: "Saving the draft artifact",
};

const TOOL_ACTIVITY_RESULT_LABELS: Record<string, string> = {
  get_channel_context: "Channel context received",
  analyze_channel: "Channel analysis received",
  get_topic_profile: "Channel profile received",
  search_web: "Search results received",
  read_sources: "Source pages received",
  compare_sources: "Source comparison received",
  find_novel_topics: "Topic candidates received",
  propose_topic_changes: "Topic proposal received",
  apply_confirmed_topic_changes: "Channel profile updated",
  explain_recommendation: "Evidence received",
  get_draft: "Draft loaded",
  create_draft: "Draft artifact saved",
  revise_draft: "Draft revision saved",
  save_draft: "Draft saved",
};

function toolActivityLabel(toolName: unknown): string {
  const normalized = typeof toolName === "string" ? toolName : "";
  if (normalized in TOOL_ACTIVITY_LABELS) return TOOL_ACTIVITY_LABELS[normalized];
  if (!normalized) return "Working with channel context";
  return `Working on ${normalized.replaceAll("_", " ")}`;
}

function completedToolActivityLabel(toolName: unknown): string {
  const normalized = typeof toolName === "string" ? toolName : "";
  return TOOL_ACTIVITY_RESULT_LABELS[normalized] ?? "Tool result received";
}

function describeAgentActivity(run: RunSummary, events: RunEvent[]): { label: string; detail: string; stage: string } {
  if (run.status === "queued") {
    return { label: "Waiting for the Studio agent", detail: "Your request is safely queued.", stage: "queued" };
  }

  const lastText = [...events].reverse().find((event) => event.event_type.startsWith("TEXT_MESSAGE_"));
  const lastToolStart = [...events].reverse().find((event) => event.event_type === "TOOL_CALL_START");
  const lastToolResult = [...events].reverse().find((event) => event.event_type === "TOOL_CALL_RESULT");
  const latestSequence = events.at(-1)?.sequence ?? 0;

  if (lastText && lastText.sequence === latestSequence) {
    return { label: "Writing the response", detail: "The answer will appear here as it is composed.", stage: "writing" };
  }
  if (lastToolStart && (!lastToolResult || lastToolStart.sequence > lastToolResult.sequence)) {
    const label = toolActivityLabel(lastToolStart.safe_payload.tool_name);
    return { label, detail: "Using only the scoped tools and context for this conversation.", stage: "tool" };
  }
  if (lastToolResult) {
    const toolName = lastToolResult.safe_payload.tool_name ?? lastToolStart?.safe_payload.tool_name;
    return {
      label: "Thinking with the latest results",
      detail: `${completedToolActivityLabel(toolName)}. The agent may continue working.`,
      stage: "thinking",
    };
  }
  return { label: "Thinking through your request", detail: "Choosing the next useful action from your channel context.", stage: "thinking" };
}

function AgentActivity({ run, events }: { run: RunSummary | null; events: RunEvent[] }) {
  const active = run?.status === "queued" || run?.status === "running";
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active, run?.id]);

  if (!run || !active) return null;
  const activity = describeAgentActivity(run, events);
  const startedAt = Date.parse(run.started_at || run.created_at || "");
  const elapsedSeconds = Number.isFinite(startedAt) ? Math.max(0, Math.floor((now - startedAt) / 1000)) : 0;

  return (
    <div className="studio-agent-activity" data-stage={activity.stage} role="status" aria-live="polite">
      <span className="studio-agent-orbit" aria-hidden="true"><span /></span>
      <span className="studio-agent-activity-copy">
        <strong>{activity.label}</strong>
        <small>{activity.detail}</small>
      </span>
      <span className="studio-agent-elapsed">{elapsedSeconds}s</span>
    </div>
  );
}

function ConversationRail({
  conversations,
  selected,
  onSelect,
  onNew,
  onDelete,
  deletingId,
  onSettings,
  onProfile,
}: {
  conversations: Conversation[];
  selected: Conversation | null;
  onSelect: (conversation: Conversation) => void;
  onNew: () => void;
  onDelete: (conversation: Conversation) => void;
  deletingId: string | null;
  onSettings: () => void;
  onProfile: () => void;
}) {
  return (
    <aside className="studio-rail" aria-label="Studio conversations">
      <div className="studio-rail-head">
        <div className="studio-symbol" aria-hidden="true">✦</div>
        <div>
          <p className="studio-overline">Content Studio</p>
          <p className="studio-rail-title">Channel desk</p>
        </div>
      </div>
      <button className="studio-new" type="button" onClick={onNew}>
        <span aria-hidden="true">＋</span> New conversation
      </button>
      <div className="studio-conversations" role="list">
        {conversations.length === 0 ? (
          <p className="studio-empty-rail">Your working threads will appear here.</p>
        ) : (
          conversations.map((conversation) => (
            <div className={`studio-conversation-row ${selected?.id === conversation.id ? "is-selected" : ""}`} key={conversation.id} role="listitem">
              <button
                className="studio-conversation"
                type="button"
                aria-current={selected?.id === conversation.id ? "page" : undefined}
                onClick={() => onSelect(conversation)}
              >
                <span className="studio-conversation-mark" aria-hidden="true" />
                <span className="studio-conversation-copy">
                  <strong>{conversation.title}</strong>
                  <small>{conversation.channel_identifier || conversation.channel_title}</small>
                </span>
              </button>
              <button
                className="studio-conversation-delete"
                type="button"
                aria-label={`Delete conversation ${conversation.title}`}
                title="Delete conversation"
                disabled={deletingId !== null}
                onClick={(event) => {
                  event.stopPropagation();
                  onDelete(conversation);
                }}
              >
                <span aria-hidden="true">{deletingId === conversation.id ? "…" : "×"}</span>
              </button>
            </div>
          ))
        )}
      </div>
      <div className="studio-rail-foot">
        <span className="studio-status-dot" aria-hidden="true" />
        <span>Private workspace</span>
        <div className="studio-rail-foot-buttons">
          <button type="button" onClick={onProfile}>Profile</button>
          <button type="button" onClick={onSettings}>Settings</button>
        </div>
      </div>
    </aside>
  );
}

function StudioSettings({ onClose }: { onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [prompt, setPrompt] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  useEffect(() => {
    dialog.current?.showModal();
    let alive = true;
    void api<{ system_prompt: string }>("/studio/api/settings").then(value => {
      if (alive) { setPrompt(value.system_prompt); setLoading(false); }
    }).catch(() => { if (alive) setNotice("Could not load settings. Close and try again."); });
    return () => { alive = false; };
  }, []);
  const save = async () => {
    setSaving(true); setNotice("");
    try {
      await api("/studio/api/settings", { method: "PATCH", headers: { "content-type": "application/json", "x-csrf-token": csrfToken() }, body: JSON.stringify({ system_prompt: prompt }) });
      setNotice("Saved. Applies to the next message in every conversation.");
    } catch { setNotice("Could not save. Your text is preserved; try again."); }
    finally { setSaving(false); }
  };
  return <dialog className="studio-settings" ref={dialog} aria-labelledby="studio-settings-title" onCancel={onClose} onClose={onClose}>
    <header><h2 id="studio-settings-title">Studio settings</h2><button type="button" aria-label="Close settings" onClick={onClose}>Close</button></header>
    <label htmlFor="studio-system-prompt">System prompt</label>
    <p>Standing instructions for all conversations in this workspace: voice, editorial preferences, topics and source criteria. These instructions are sent to the configured model. Security rules still apply.</p>
    <textarea id="studio-system-prompt" autoFocus value={prompt} maxLength={12000} disabled={loading} onChange={event => { setPrompt(event.target.value); setNotice(""); }} placeholder="How should the Studio agent work with you?" />
    <footer><span>{prompt.length.toLocaleString()} / 12,000 · Leave empty to use defaults.</span><button type="button" className="studio-copy" disabled={loading || saving} onClick={() => void save()}>{saving ? "Saving…" : "Save instructions"}</button></footer>
    {notice && <p role="status">{notice}</p>}
  </dialog>;
}

function renderInlineMarkdown(line: string, key: number) {
  let text = line.replace(/!\[([^\]]*)\]\([^)]*\)/g, "");
  text = text.replace(/<[a-zA-Z\/][^>]*>/g, "");
  const parts: React.ReactNode[] = [];
  let lastIndex = 0;
  const pattern = /(\*\*[^*]+\*\*|\*[^*]+\*|~~[^~]+~~|`[^`]+`|\[([^\]]+)\]\((https?:\/\/[^)]+)\)|\[([^\]]+)\]\([^)]+\)|> .+)/g;
  let match: RegExpExecArray | null;
  let idx = 0;
  while ((match = pattern.exec(text)) !== null) {
    const start = match.index;
    if (start > lastIndex) parts.push(<span key={`t-${key}-${idx++}`}>{text.slice(lastIndex, start)}</span>);
    const token = match[0];
    if (token.startsWith("**")) parts.push(<strong key={`b-${key}-${idx++}`}>{token.slice(2, -2)}</strong>);
    else if (token.startsWith("~~")) parts.push(<s key={`s-${key}-${idx++}`}>{token.slice(2, -2)}</s>);
    else if (token.startsWith("`")) parts.push(<code key={`c-${key}-${idx++}`}>{token.slice(1, -1)}</code>);
    else if (token.startsWith("[") && match[3]) parts.push(<a key={`a-${key}-${idx++}`} href={match[3]} target="_blank" rel="noopener noreferrer">{match[2]}</a>);
    else if (token.startsWith("[") && match[4]) parts.push(<span key={`l-${key}-${idx++}`}>{match[4]}</span>);
    else if (token.startsWith("*") && !token.startsWith("**")) parts.push(<em key={`i-${key}-${idx++}`}>{token.slice(1, -1)}</em>);
    else if (token.startsWith("> ")) parts.push(<blockquote key={`q-${key}-${idx++}`}><span>{token.slice(2)}</span></blockquote>);
    else parts.push(<span key={`u-${key}-${idx++}`}>{token}</span>);
    lastIndex = pattern.lastIndex;
  }
  if (lastIndex < text.length) parts.push(<span key={`t-${key}-${idx++}`}>{text.slice(lastIndex)}</span>);
  if (parts.length === 0) return <span key={key}>{line}</span>;
  return <span key={key}>{parts}</span>;
}

function DraftMarkdownPreview({ text }: { text: string }) {
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

function DraftPanel({
  conversationId,
  seedDraft,
  open,
  onClose,
  watchForAgentChanges,
  refreshToken,
}: {
  conversationId: string | null;
  seedDraft: Draft | null;
  open: boolean;
  onClose: () => void;
  watchForAgentChanges: boolean;
  refreshToken: number;
}) {
  const [draft, setDraft] = useState<Draft | null>(seedDraft);
  const [sources, setSources] = useState<{ id: string; title: string; url: string }[]>([]);
  const [panelWidth, setPanelWidth] = useState(() => {
    try { return Math.max(320, Math.min(720, Number(localStorage.getItem("studio-artifact-width")) || 360)); }
    catch { return 360; }
  });
  useEffect(() => {
    document.documentElement.style.setProperty("--studio-draft-width", `${panelWidth}px`);
    try { localStorage.setItem("studio-artifact-width", String(panelWidth)); } catch { /* Optional preference. */ }
  }, [panelWidth]);
  const [versions, setVersions] = useState<DraftVersion[]>([]);
  const [saveState, setSaveState] = useState<"saved" | "saving" | "conflict" | "error">("saved");
  const [copied, setCopied] = useState(false);
  const [copyNote, setCopyNote] = useState("");
  const [previewOpen, setPreviewOpen] = useState(false);
  const [conflict, setConflict] = useState<{ server: Draft; localBody: string; localTitle: string } | null>(null);
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  // Choosing an older version in the selector shows it read-only; only Restore
  // writes it back as the current draft.
  const viewedVersion = versions.find((item) => item.version === selectedVersion);
  const viewingOld = !!(draft && viewedVersion && selectedVersion !== draft.current_version);
  const shownBody = viewingOld && viewedVersion ? viewedVersion.body : (draft?.body ?? "");
  const hydrated = useRef(false);
  const draftRef = useRef<Draft | null>(seedDraft);
  const saveStateRef = useRef(saveState);
  const localChange = useRef(0);
  const saveTimer = useRef<number | undefined>(undefined);
  const loadedConversation = useRef<string | null>(conversationId);

  useEffect(() => { draftRef.current = draft; }, [draft]);
  useEffect(() => { saveStateRef.current = saveState; }, [saveState]);

  const load = () => {
    const conversationChanged = loadedConversation.current !== conversationId;
    loadedConversation.current = conversationId;
    if (!conversationId) {
      setDraft(null);
      setVersions([]);
      return Promise.resolve();
    }
    hydrated.current = false;
    if (conversationChanged) {
      localChange.current = 0;
      setConflict(null);
    }
    return api<{ draft: Draft | null; sources?: typeof sources }>(`/studio/api/conversations/${conversationId}/draft`)
      .then((payload) => {
        if (loadedConversation.current !== conversationId) return;
        setSources(payload.sources ?? []);
        setDraft((current) => {
          if (!conversationChanged && current && localChange.current > 0 && current.body !== payload.draft?.body) return current;
          return payload.draft;
        });
        if (payload.draft) {
          setSelectedVersion(payload.draft.current_version);
          return api<{ versions: DraftVersion[] }>(`/studio/api/drafts/${payload.draft.id}/versions`).then((history) => setVersions(history.versions));
        }
        setVersions([]);
        setSelectedVersion(null);
      })
      .finally(() => { hydrated.current = true; });
  };

  useEffect(() => {
    void load().catch(() => setSaveState("error"));
    // A new conversation or completed agent run requires one authoritative
    // refresh. Continuous polling is handled separately and only while an
    // agent run can actually be writing draft versions.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId, refreshToken]);

  useEffect(() => {
    if (!conversationId || !watchForAgentChanges) return;
    const poll = window.setInterval(() => {
      if (document.visibilityState === "hidden" || !hydrated.current || saveStateRef.current === "saving") return;
      void api<{ draft: Draft | null; sources?: typeof sources }>(`/studio/api/conversations/${conversationId}/draft`).then((payload) => {
        if (loadedConversation.current !== conversationId) return;
        setSources(payload.sources ?? []);
        const next = payload.draft;
        const previous = draftRef.current;
        if (next && (!previous || previous.id !== next.id || previous.current_version !== next.current_version || previous.updated_at !== next.updated_at)) {
          void api<{ versions: DraftVersion[] }>(`/studio/api/drafts/${next.id}/versions`).then((history) => setVersions(history.versions)).catch(() => undefined);
        }
        if (next && !(previous && localChange.current > 0 && previous.body !== next.body)) setSelectedVersion(next.current_version);
        setDraft((current) => {
          if (!next) return current;
          // Preserve unsaved local typing while a streamed agent run writes a
          // new server version.
          if (current && localChange.current > 0 && current.body !== next.body) return current;
          return next;
        });
      }).catch(() => undefined);
    }, 1800);
    return () => window.clearInterval(poll);
  }, [conversationId, watchForAgentChanges]);

  const save = (local: Draft, changeId: number) => {
    setSaveState("saving");
    void api<{ draft: Draft }>(`/studio/api/drafts/${local.id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json", "x-csrf-token": csrfToken() },
      body: JSON.stringify({ expected_revision: local.revision, body: local.body, working_title: local.working_title }),
    })
      .then((payload) => {
        if (loadedConversation.current !== local.conversation_id) return;
        const latest = localChange.current === changeId;
        setVersions((items) => items.some((item) => item.version === payload.draft.current_version) ? items : [...items, { id: Date.now(), draft_id: payload.draft.id, version: payload.draft.current_version, body: payload.draft.body, origin: "user_edit", instruction: "", character_count: payload.draft.character_count, created_at: payload.draft.updated_at }]);
        setDraft((current) => {
          if (!current || localChange.current === changeId) {
            if (localChange.current === changeId) localChange.current = 0;
            return payload.draft;
          }
          return { ...current, revision: payload.draft.revision };
        });
        if (latest) setSaveState("saved");
        setConflict(null);
      })
      .catch((error: unknown) => {
        if (error instanceof StudioApiError && error.status === 409 && error.payload.server_draft) {
          setConflict({ server: error.payload.server_draft, localBody: local.body, localTitle: local.working_title });
          setSaveState("conflict");
        } else setSaveState("error");
      });
  };

  useEffect(() => {
    if (!draft || !hydrated.current || localChange.current === 0) return;
    if (saveTimer.current !== undefined) window.clearTimeout(saveTimer.current);
    const changeId = localChange.current;
    saveTimer.current = window.setTimeout(() => save(draft, changeId), 650);
    return () => { if (saveTimer.current !== undefined) window.clearTimeout(saveTimer.current); };
  }, [draft?.body, draft?.working_title, draft?.id]);

  const edit = (field: "body" | "working_title", value: string) => {
    localChange.current += 1;
    setDraft((current) => {
      if (!current) return current;
      if (field === "working_title") {
        return { ...current, working_title: value.slice(0, 160), copied_at: null };
      }
      const plain = plainFromMarkdown(value);
      return {
        ...current,
        body: value,
        body_plain: plain,
        // The server-rendered HTML belongs to the previous body; until the
        // edit is saved, a rich copy must fall back to plain text.
        body_html: undefined,
        character_count: Array.from(plain).length,
        plain_character_count: Array.from(plain).length,
        over_limit: Array.from(plain).length > 4096,
        warning_threshold: Array.from(plain).length >= 3800,
        copied_at: null,
      };
    });
    setSaveState("saving");
    setCopied(false);
  };

  const copy = async () => {
    if (!draft || draft.over_limit) return;
    // Both flavours come from the Markdown shown in the editor (the current
    // draft, or the version being viewed), saved or not.
    // text/plain carries Telegram's own markup (**bold**, __italic__), which
    // every Telegram app converts on send even when it ignores rich flavours;
    // text/html carries real tags and links for clients that read them.
    const plain = telegramMarkupFromMarkdown(shownBody);
    const html = htmlFromMarkdown(shownBody);
    try {
      // 1. Copy-event handler: both flavours under our control, synchronous
      //    inside the click, no permission prompt.
      // 2. Rendered selection: browser-serialised flavours (adds RTF in Safari).
      // 3. Async Clipboard API; last because Safari rejects it after an await
      //    and plain-HTTP origins do not offer it.
      let done = copyRichText(plain, html) || copyRenderedSelection(html);
      let rich = done;
      if (!done) {
        const clip = navigator.clipboard as unknown as { write?: (items: unknown[]) => Promise<void>; writeText?: (text: string) => Promise<void> } | undefined;
        const ClipboardItemCtor = (window as unknown as { ClipboardItem?: new (items: Record<string, Blob>) => unknown }).ClipboardItem;
        if (clip?.write && ClipboardItemCtor) {
          await clip.write([new ClipboardItemCtor({ "text/plain": new Blob([plain], { type: "text/plain" }), "text/html": new Blob([html], { type: "text/html" }) })]);
          done = rich = true;
        } else if (clip?.writeText) {
          await clip.writeText(plain);
          done = true;
        }
      }
      if (!done) throw new Error("Clipboard unavailable");
      setCopied(true);
      // Say which flavour landed so a plain-text paste can be diagnosed at once.
      setCopyNote(rich ? "Copied. Formatting travels as rich text and as Telegram markup." : "Copied as Telegram markup only (this browser blocks rich copy).");
      setDraft((current) => current ? { ...current, copied_at: new Date().toISOString() } : current);
      window.setTimeout(() => { setCopied(false); setCopyNote(""); }, 3200);
      // Record the copy on the saved revision; failures here must not undo a successful copy.
      if (saveState === "saved") void api(`/studio/api/drafts/${draft.id}/copied`, { method: "POST", headers: { "x-csrf-token": csrfToken() } }).catch(() => undefined);
    } catch {
      setSaveState("error");
    }
  };

  const restore = (version: DraftVersion) => {
    if (!draft) return;
    setSaveState("saving");
    void api<{ draft: Draft }>(`/studio/api/drafts/${draft.id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json", "x-csrf-token": csrfToken() },
      body: JSON.stringify({ expected_revision: draft.revision, restore_version: version.version }),
    }).then((payload) => {
      localChange.current = 0;
      setDraft(payload.draft);
      setSaveState("saved");
      setConflict(null);
      // Restoring creates a new version on the server. Refresh the list so the
      // selector can show it; an unknown value would make the <select> fall
      // back to its first option while the state pointed elsewhere.
      return api<{ versions: DraftVersion[] }>(`/studio/api/drafts/${payload.draft.id}/versions`)
        .then((history) => setVersions(history.versions))
        .catch(() => setVersions((items) => items.some((item) => item.version === payload.draft.current_version) ? items : [...items, { id: Date.now(), draft_id: payload.draft.id, version: payload.draft.current_version, body: payload.draft.body, origin: "user_edit", instruction: `Restored version ${version.version}`, character_count: payload.draft.character_count, created_at: payload.draft.updated_at }]))
        .finally(() => setSelectedVersion(payload.draft.current_version));
    }).catch((error: unknown) => {
      if (error instanceof StudioApiError && error.status === 409 && error.payload.server_draft) {
        setConflict({ server: error.payload.server_draft, localBody: draft.body, localTitle: draft.working_title });
        setSaveState("conflict");
      } else {
        setSaveState("error");
      }
    });
  };

  const keepLocal = () => {
    if (!conflict) return;
    localChange.current += 1;
    setDraft({ ...conflict.server, body: conflict.localBody, working_title: conflict.localTitle });
    setConflict(null);
    setSaveState("saving");
  };

  const useServer = () => {
    if (!conflict) return;
    localChange.current = 0;
    setDraft(conflict.server);
    setConflict(null);
    setSaveState("saved");
  };

  const sourceLinks = new Map<string, { url: string; title: string }>();
  const addSource = (id: string, url: string, label: string) => {
    try {
      const parsed = new URL(url);
      if (id && ["http:", "https:"].includes(parsed.protocol) && !parsed.username && !parsed.password)
        sourceLinks.set(id, { url: parsed.href, title: label.trim() || parsed.hostname });
    } catch { /* Malformed model metadata cannot crash the artifact. */ }
  };
  for (const item of draft?.web_evidence ?? []) {
    const id = String(item.source_id ?? item.id ?? "");
    const url = String(item.url ?? "");
    addSource(id, url, String(item.title ?? item.headline ?? ""));
  }
  for (const source of sources) {
    addSource(source.id, source.url, source.title);
  }
  const clickableSources = Array.from(new Map(
    (draft?.source_ids ?? []).flatMap((id) => {
      const source = sourceLinks.get(id);
      return source ? [[source.url, source] as const] : [];
    }),
  ).values());

  return (
    <aside className={`studio-draft ${open ? "is-open" : ""}`} aria-label="Draft artifact">
      <div className="studio-draft-resizer" role="separator" aria-label="Resize draft panel" aria-orientation="vertical" aria-valuemin={320} aria-valuemax={720} aria-valuenow={panelWidth} tabIndex={0}
        onPointerDown={(event) => { event.currentTarget.setPointerCapture(event.pointerId); }}
        onPointerMove={(event) => { if (event.currentTarget.hasPointerCapture(event.pointerId)) setPanelWidth(Math.max(320, Math.min(720, window.innerWidth - event.clientX))); }}
        onPointerUp={(event) => event.currentTarget.releasePointerCapture(event.pointerId)}
        onKeyDown={(event) => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) { event.preventDefault(); setPanelWidth((width) => event.key === "Home" ? 320 : event.key === "End" ? 720 : Math.max(320, Math.min(720, width + (event.key === "ArrowLeft" ? 20 : -20)))); } }}
        onDoubleClick={() => setPanelWidth(360)} />
      <div className="studio-panel-heading">
        <div>
          <p className="studio-overline">Artifact</p>
          <h2>Draft workspace</h2>
        </div>
        <div className="studio-panel-actions">
          <span className={`studio-save-state is-${saveState}`} role="status">{saveState === "saving" ? "Saving…" : saveState === "conflict" ? "Needs review" : saveState === "error" ? "Retry needed" : "Saved"}</span>
          <button type="button" className="studio-draft-close" onClick={onClose} aria-label="Close draft">×</button>
        </div>
      </div>
      {!draft ? (
        <div className="studio-draft-empty">
          <div className="studio-draft-glyph" aria-hidden="true">∿</div>
          <h3>Your draft will live here</h3>
          <p>Ask the agent to find a story and shape a source-backed, Telegram-ready post. The artifact stays editable while the conversation continues.</p>
        </div>
      ) : (
        <div className="studio-draft-content">
          <label className="studio-draft-title">Artifact title<input aria-label="Artifact title" value={draft.working_title} onChange={(event) => edit("working_title", event.target.value)} maxLength={160} placeholder="Untitled draft" /><span className="studio-draft-title-hint">Kept for search and cross-checking. Not copied to the post.</span></label>
          {viewingOld && <p className="studio-draft-viewing" role="status">Viewing v{selectedVersion} (read-only). Restore makes it the current draft.</p>}
          {previewOpen
            ? <div className="studio-draft-preview" role="region" aria-label="Post preview"><div className="studio-markdown"><DraftMarkdownPreview text={shownBody} /></div></div>
            : <textarea className="studio-draft-editor" aria-label="Telegram post — headline and body" value={shownBody} readOnly={viewingOld} onChange={(event) => edit("body", event.target.value)} />}
          <div className={`studio-char-count ${draft.over_limit ? "is-over" : draft.warning_threshold ? "is-warning" : ""}`}>
            <span>{(draft.character_count ?? Array.from(plainFromMarkdown(draft.body)).length).toLocaleString()} / 4,096 plain-text characters</span>
            <span>{draft.over_limit ? "Copy blocked" : draft.warning_threshold ? "Near Telegram limit" : "Telegram ready"}</span>
          </div>
          {conflict && <div className="studio-conflict" role="alert"><strong>This draft changed elsewhere.</strong><span>Your local text is preserved.</span><div><button type="button" onClick={keepLocal}>Keep my text</button><button type="button" onClick={useServer}>Use server version</button></div></div>}
          {clickableSources.length > 0 && <div className="studio-draft-notes"><strong>Sources</strong><div className="studio-source-chips">{clickableSources.map((source) => <a key={source.url} href={source.url} target="_blank" rel="noopener noreferrer">{source.title}</a>)}</div></div>}
          <div className="studio-draft-toolbar"><button type="button" className="studio-copy" onClick={() => void copy()} disabled={draft.over_limit}>{copied ? "Copied" : "Copy post"}</button><button type="button" className="studio-draft-mode" aria-pressed={previewOpen} onClick={() => setPreviewOpen((open) => !open)}>{previewOpen ? "Edit" : "Preview"}</button>{copyNote && <span className="studio-copy-note" role="status">{copyNote}</span>}<label className="studio-version-select">Version<select aria-label="Draft version" value={selectedVersion ?? draft.current_version} onChange={(event) => setSelectedVersion(Number(event.target.value))}>{versions.map((version) => <option key={version.version} value={version.version}>v{version.version} · {version.origin}</option>)}</select><button type="button" className="studio-restore" onClick={() => { const version = versions.find((item) => item.version === selectedVersion); if (version && version.version !== draft.current_version) restore(version); }} disabled={selectedVersion === null || selectedVersion === draft.current_version}>Restore</button></label></div>
        </div>
      )}
    </aside>
  );
}

function ProfilePrimer({ bootstrap, onProfile, onBootstrap }: { bootstrap: Bootstrap; onProfile: () => void; onBootstrap: (next: Bootstrap) => void }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const consent = bootstrap.consent;
  const grant = () => {
    setBusy(true);
    setMessage("");
    void api<{ consent: Bootstrap["consent"]; profile: Bootstrap["profile"] }>("/studio/api/consent", {
      method: "POST",
      headers: { "content-type": "application/json", "x-csrf-token": csrfToken() },
      body: JSON.stringify({ confirm: true, configuration_fingerprint: consent.configuration_fingerprint }),
    })
      .then(() =>
        // Consent changes what the primer shows, so reload the bootstrap
        // payload instead of leaving the consent card on screen.
        api<Bootstrap>("/studio/api/bootstrap")
          .then((next) => onBootstrap(next))
          .catch(() => setMessage("Consent granted. Reload the page to continue.")),
      )
      .catch((error: unknown) => setMessage(error instanceof Error ? error.message : "Consent could not be saved."))
      .finally(() => setBusy(false));
  };

  if (consent.required && !consent.granted) {
    return (
      <section className="studio-primer studio-primer-consent" aria-label="OpenRouter consent">
        <div>
          <p className="studio-overline">One-time permission</p>
          <h2>{consent.disclosure?.title ?? "Allow OpenRouter for Studio"}</h2>
          <p>{consent.disclosure?.message ?? "Studio sends bounded channel evidence to OpenRouter for agent assistance."}</p>
        </div>
        <button type="button" className="studio-primer-action" onClick={grant} disabled={busy}>{busy ? "Saving…" : "Allow and analyze"}</button>
        {message && <p className="studio-primer-error" role="alert">{message}</p>}
      </section>
    );
  }
  const profile = bootstrap.profile;
  const hasProfile = !!(profile && (profile.topics_text?.trim() || profile.editorial_text?.trim() || profile.style_text?.trim()));
  if (!hasProfile) {
    return (
      <section className="studio-primer" aria-label="Channel profile status">
        <p role="status">No channel profile yet. Build it from your posts or write the guidelines yourself.</p>
        <button type="button" onClick={onProfile}>Profile</button>
      </section>
    );
  }
  // Chips show the topic name only; the scope after the dash belongs in the dialog.
  const topicName = (line: string) => {
    const name = line.split(/\s[—–-]\s|:\s/)[0].trim();
    return name.length > 48 ? `${name.slice(0, 47)}…` : name;
  };
  const topicLines = (profile.topics_text ?? "").split("\n").filter((line) => line.trim());
  const editorialCount = (profile.editorial_text ?? "").split("\n").filter((l) => l.trim()).length;
  const styleCount = (profile.style_text ?? "").split("\n").filter((l) => l.trim()).length;
  const topicsCount = topicLines.length;
  const rulesCount = editorialCount + styleCount;
  const remainingTopics = topicsCount > 4 ? `+${topicsCount - 4}` : null;
  const chips = topicLines.slice(0, 4).map(topicName);
  return (
    <section className="studio-primer studio-primer-profile" aria-label="Channel profile status">
      <div>
        <p className="studio-overline">Channel profile · v{profile.version}</p>
        <p>{topicsCount} topics · {rulesCount} rules. The agent receives these guidelines with every message.</p>
      </div>
      <div className="studio-topic-chips" aria-label="Topics">
        {chips.map((topic, index) => <span key={`${index}-${topic}`} title={topicLines[index]}>{topic}</span>)}
        {remainingTopics && <span>{remainingTopics}</span>}
      </div>
      <button type="button" onClick={onProfile}>Profile</button>
    </section>
  );
}

function formatRunDuration(durationMs: number | null | undefined): string {
  if (durationMs === null || durationMs === undefined) return "—";
  if (durationMs < 1000) return `${durationMs} ms`;
  const seconds = durationMs / 1000;
  return seconds < 60 ? `${seconds.toFixed(1)} s` : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

function formatRunTokens(usage: RunUsage | undefined): string {
  if (!usage) return "—";
  return usage.total_tokens.toLocaleString();
}

function RunDetailsPanel({ run }: { run: RunSummary | null }) {
  const [open, setOpen] = useState(false);
  const [details, setDetails] = useState<RunDetails | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = () => {
    if (!run) return;
    setLoading(true);
    setError("");
    void api<{ run: RunSummary; details: RunDetails }>(`/studio/api/runs/${run.id}/details`)
      .then((payload) => setDetails(payload.details))
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Run details could not load."))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    setDetails(null);
    setError("");
    if (open && run) load();
    // The panel is intentionally fetched only when opened. Run metadata is
    // operational context, not part of the main chat payload.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.id, open]);

  if (!run) return null;
  const shown = details ?? {
    provider: run.provider,
    requested_model: run.requested_model,
    actual_model: run.actual_model,
    prompt_version: "—",
    status: run.status,
    stage: run.stage,
    duration_ms: run.duration_ms,
    usage: run.usage,
    activity: { event_count: 0, tool_call_count: 0, result_count: 0, source_count: 0, story_count: 0, cache_hit_count: 0, degraded: false, references: {} },
    error: run.error_code ? { code: run.error_code, message: run.error_message ?? "The run could not complete.", retryable: false } : null,
  };
  const cost = shown.usage.estimated_cost_usd;

  return (
    <section className={`studio-run-details ${open ? "is-open" : ""}`} aria-label="Run details">
      <button
        type="button"
        className="studio-run-details-toggle"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span><span className="studio-run-details-mark" aria-hidden="true" />Run details</span>
        <small>{run.status} · {formatRunDuration(shown.duration_ms)}</small>
        <span className="studio-run-details-chevron" aria-hidden="true">{open ? "⌃" : "⌄"}</span>
      </button>
      {open && (
        <div className="studio-run-details-body">
          {loading && <span className="studio-run-details-muted" role="status">Loading operational summary…</span>}
          {error && <span className="studio-run-details-error" role="alert">{error}</span>}
          {!loading && !error && (
            <>
              <div className="studio-run-meta-grid">
                <div><span>Provider</span><strong>{shown.provider}</strong></div>
                <div><span>Model</span><strong>{shown.actual_model || shown.requested_model}</strong></div>
                <div><span>Duration</span><strong>{formatRunDuration(shown.duration_ms)}</strong></div>
                <div><span>Tokens</span><strong>{formatRunTokens(shown.usage)}</strong></div>
                <div><span>Estimated cost</span><strong>{cost === undefined ? "Unavailable" : `$${cost.toFixed(4)}`}</strong></div>
                <div><span>Tool calls</span><strong>{shown.activity.tool_call_count}</strong></div>
              </div>
              <div className="studio-run-details-foot">
                <span>{shown.activity.source_count} sources · {shown.activity.story_count} stories</span>
                <span>{shown.activity.cache_hit_count} cache hits · {shown.prompt_version}</span>
              </div>
              {shown.activity.degraded && <span className="studio-run-details-warning">Research was degraded for this run.</span>}
              {shown.error && <div className="studio-run-details-error"><strong>{shown.error.code}</strong><span>{shown.error.message}</span></div>}
            </>
          )}
        </div>
      )}
    </section>
  );
}

function StudioThread({
  conversation,
  seedRun,
  onRunActivityChange,
  onRunFinished,
}: {
  conversation: Conversation;
  seedRun: RunSummary | null;
  onRunActivityChange: (active: boolean) => void;
  onRunFinished: () => void;
}) {
  const agent = useMemo(
    () =>
      new HttpAgent({
        url: "/studio/api/agent",
        threadId: conversation.id,
        headers: {
          "x-csrf-token": csrfToken(),
        },
      }),
    [conversation.id],
  );
  const runtime = useAgUiRuntime({
    agent,
    showThinking: false,
    onError: (error) => console.error("Studio run failed", error),
  });
  const [messages, setMessages] = useState<PersistedMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [recoveredRun, setRecoveredRun] = useState<RunSummary | null>(seedRun);
  const [recoveredEvents, setRecoveredEvents] = useState<RunEvent[]>([]);

  useEffect(() => {
    onRunActivityChange(recoveredRun?.status === "queued" || recoveredRun?.status === "running");
  }, [onRunActivityChange, recoveredRun?.status]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    void api<{ messages: PersistedMessage[] }>(`/studio/api/conversations/${conversation.id}/messages`)
      .then((payload) => {
        if (!alive) return;
        setMessages(payload.messages);
        runtime.thread.reset(asThreadMessages(payload.messages));
      })
      .catch((error: unknown) => {
        if (alive) console.error("Could not load Studio history", error);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [conversation.id, runtime]);

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    let cursor = 0;
    setRecoveredRun(seedRun);

    const restoreMessages = () => api<{ messages: PersistedMessage[] }>(`/studio/api/conversations/${conversation.id}/messages`)
      .then((payload) => {
        if (!alive) return;
        setMessages(payload.messages);
        runtime.thread.reset(asThreadMessages(payload.messages));
      });

    let consecutiveErrors = 0;
    const poll = (run: RunSummary) => {
      void api<{ run: RunSummary; events: RunEvent[] }>(`/studio/api/runs/${run.id}/events?after=${cursor}`)
        .then((payload) => {
          if (!alive) return;
          consecutiveErrors = 0;
          setRecoveredRun(payload.run);
          if (payload.events.length > 0) {
            cursor = payload.events[payload.events.length - 1].sequence;
            setRecoveredEvents((current) => [...current, ...payload.events].slice(-24));
          }
          if (payload.run.status === "queued" || payload.run.status === "running") {
            timer = window.setTimeout(() => poll(payload.run), 850);
          } else {
            void restoreMessages();
            onRunFinished();
          }
        })
        .catch((error: unknown) => {
          if (!alive) return;
          const status = error instanceof StudioApiError ? error.status : 0;
          if (isTerminalPollStatus(status)) {
            setRecoveredRun(null);
            onRunFinished();
            return;
          }
          consecutiveErrors += 1;
          if (shouldStopPollingAfterErrors(consecutiveErrors)) {
            setRecoveredRun(null);
            onRunFinished();
            return;
          }
          const delay = nextPollDelay(consecutiveErrors);
          timer = window.setTimeout(() => poll(run), delay);
        });
    };

    setRecoveredEvents([]);
    const runHint = (id: string, status: RunSummary["status"]): RunSummary => ({
      id,
      conversation_id: conversation.id,
      status,
      stage: status,
      provider: "openrouter",
      requested_model: "",
      actual_model: null,
      usage: { requests: 0, tool_calls: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0 },
      error_code: null,
      error_message: null,
      created_at: new Date().toISOString(),
      started_at: status === "running" ? new Date().toISOString() : null,
      finished_at: null,
      duration_ms: null,
    });

    const subscription = agent.subscribe({
      onRunInitialized: ({ input }) => {
        if (!input.runId) return;
        setRecoveredEvents([]);
        setRecoveredRun(runHint(input.runId, "queued"));
      },
      onRunStartedEvent: ({ event, input }) => {
        const canonicalRunId = event.runId || input.runId;
        if (!canonicalRunId) return;
        const hint = runHint(canonicalRunId, "running");
        setRecoveredRun(hint);
        poll(hint);
      },
      onRunFailed: () => {
        // Keep the run identity so its durable error remains inspectable.
        onRunFinished();
      },
    });
    const discover = seedRun
      ? Promise.resolve({ run: seedRun })
      : api<{ run: RunSummary | null }>(`/studio/api/conversations/${conversation.id}/active-run`);
    void discover.then((payload) => {
      if (!alive || !payload.run) return;
      setRecoveredRun(payload.run);
      poll(payload.run);
    }).catch(() => undefined);

    return () => {
      alive = false;
      subscription.unsubscribe();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [agent, conversation.id, onRunFinished, runtime, seedRun?.id]);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div className="studio-thread-wrap">
        {loading && <div className="studio-loading" role="status">Restoring this conversation…</div>}
        <RunDetailsPanel run={recoveredRun} />
        <ThreadPrimitive.Root className="studio-thread">
          <ThreadPrimitive.Viewport className="studio-viewport">
            {!loading && (
              <ThreadPrimitive.Empty>
              <div className="studio-welcome">
                <span className="studio-welcome-kicker">Ready when you are</span>
                <h2>What should we make clearer today?</h2>
                <p>I’ll start with the selected channel’s history and keep the working context in this conversation.</p>
                <div className="studio-prompts" aria-label="Suggested prompts">
                  <span>“Show me what performs best”</span>
                  <span>“Help me find a fresh angle”</span>
                </div>
              </div>
              </ThreadPrimitive.Empty>
            )}
            <ThreadPrimitive.Messages components={{ Message: StudioMessage }} />
          </ThreadPrimitive.Viewport>
          <div className="studio-composer-stack">
            <AgentActivity run={recoveredRun} events={recoveredEvents} />
            <ComposerPrimitive.Root className="studio-composer">
              <ComposerPrimitive.Input
                aria-label="Message the Studio agent"
                placeholder="Tell the agent what you want to explore…"
                autoFocus
              />
              <ComposerPrimitive.Send className="studio-send" aria-label="Send message">
                <span aria-hidden="true">↗</span>
              </ComposerPrimitive.Send>
            </ComposerPrimitive.Root>
          </div>
        </ThreadPrimitive.Root>
      </div>
    </AssistantRuntimeProvider>
  );
}

function StudioApp() {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [selected, setSelected] = useState<Conversation | null>(null);
  const [error, setError] = useState("");
  const [draftOpen, setDraftOpen] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [agentRunActive, setAgentRunActive] = useState(false);
  const [draftRefreshToken, setDraftRefreshToken] = useState(0);
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleText, setTitleText] = useState("");
  const [titleError, setTitleError] = useState("");

  const handleRunFinished = useCallback(() => {
    setAgentRunActive(false);
    setDraftRefreshToken((current) => current + 1);
    refresh();
  }, []);

  useEffect(() => {
    setAgentRunActive(false);
  }, [selected?.id]);

  const refresh = () => {
    void api<Bootstrap>("/studio/api/bootstrap")
      .then((payload) => {
        setBootstrap(payload);
        setSelected((current) =>
          payload.conversations.find((conversation) => conversation.id === current?.id) ??
            payload.current_conversation,
        );
      })
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Studio could not load."));
  };

  useEffect(refresh, []);
  useEffect(() => {
    if (agentRunActive) {
      const timer = window.setTimeout(refresh, 1500);
      return () => window.clearTimeout(timer);
    }
  }, [agentRunActive]);
  useEffect(() => { setEditingTitle(false); setTitleError(""); }, [selected?.id]);

  const rename = () => {
    if (!selected || !titleText.trim()) return;
    void api<{ conversation: Conversation }>(`/studio/api/conversations/${selected.id}`, {
      method: "PATCH", headers: { "content-type": "application/json", "x-csrf-token": csrfToken() },
      body: JSON.stringify({ title: titleText.trim() }),
    }).then(({ conversation }) => { setSelected(conversation); setEditingTitle(false); setTitleError(""); refresh(); })
      .catch((reason: unknown) => setTitleError(reason instanceof Error ? reason.message : "Could not rename."));
  };

  const createConversation = () => {
    if (!bootstrap?.selected_channel_id) return;
    void api<{ conversation: Conversation }>("/studio/api/conversations", {
      method: "POST",
      headers: { "content-type": "application/json", "x-csrf-token": csrfToken() },
      body: JSON.stringify({ channel_id: bootstrap.selected_channel_id }),
    })
      .then((payload) => {
        setSelected(payload.conversation);
        refresh();
      })
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Could not create conversation."));
  };

  const deleteConversation = (conversation: Conversation) => {
    if (!window.confirm(`Delete “${conversation.title}” permanently? Its Studio messages, drafts, research, and run history will be removed. Telegram channel data will not be affected.`)) return;
    setDeletingId(conversation.id);
    void api<{ conversation_id: string; deleted: boolean }>(`/studio/api/conversations/${encodeURIComponent(conversation.id)}`, {
      method: "DELETE",
      headers: { "x-csrf-token": csrfToken() },
    })
      .then(() => {
        setSelected((current) => current?.id === conversation.id ? null : current);
        setBootstrap((current) => {
          if (!current) return current;
          const conversations = current.conversations.filter((item) => item.id !== conversation.id);
          const removedCurrent = current.current_conversation?.id === conversation.id;
          return {
            ...current,
            conversations,
            current_conversation: removedCurrent ? (conversations[0] ?? null) : current.current_conversation,
            draft: removedCurrent ? null : current.draft,
            active_run: removedCurrent ? null : current.active_run,
          };
        });
        refresh();
      })
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Could not delete conversation."))
      .finally(() => setDeletingId(null));
  };

  if (error) return <div className="studio-error" role="alert"><strong>Studio could not load.</strong><span>{error}</span><button type="button" onClick={() => { setError(""); refresh(); }}>Try again</button></div>;
  if (!bootstrap) return <div className="studio-loading studio-page-loading" role="status">Opening your workspace…</div>;
  if (!bootstrap.setup.ready) return null;

  return (
    <div className="studio-app">
      <ConversationRail conversations={bootstrap.conversations} selected={selected} onSelect={setSelected} onNew={createConversation} onDelete={deleteConversation} deletingId={deletingId} onSettings={() => setSettingsOpen(true)} onProfile={() => setProfileOpen(true)} />
      {settingsOpen && <StudioSettings onClose={() => setSettingsOpen(false)} />}
      {profileOpen && bootstrap.selected_channel_id && (
        <ChannelProfileDialog
          channelId={bootstrap.selected_channel_id}
          onClose={() => setProfileOpen(false)}
          onSaved={(profile) => {
            setBootstrap((current) => current ? { ...current, profile, profile_status: (profile.topics_text?.trim() || profile.editorial_text?.trim() || profile.style_text?.trim()) ? "ready" : "not_built" } : current);
          }}
        />
      )}
      <main className="studio-main">
        <header className="studio-topbar">
          <div>
            <p className="studio-overline">{selected?.channel_identifier ?? "Channel"}</p>
            {editingTitle ? <form className="studio-title-editor" onSubmit={(event) => { event.preventDefault(); rename(); }}><input autoFocus aria-label="Conversation title" maxLength={160} value={titleText} onChange={(event) => setTitleText(event.target.value)} onKeyDown={(event) => { if (event.key === "Escape") setEditingTitle(false); }} /><button type="submit" disabled={!titleText.trim()}>Save</button><button type="button" onClick={() => setEditingTitle(false)}>Cancel</button></form> : <div className="studio-title-row"><h1>{selected?.title ?? "Content Studio"}</h1>{selected && <button type="button" aria-label="Rename conversation" onClick={() => { setTitleText(selected.title); setEditingTitle(true); }}>Rename</button>}</div>}
            {titleError && <p role="alert">{titleError}</p>}
          </div>
          <button type="button" className="studio-draft-toggle" onClick={() => setDraftOpen(true)}>Draft</button>
          <button type="button" className="studio-settings-mobile" onClick={() => setProfileOpen(true)}>Profile</button>
          <button type="button" className="studio-settings-mobile" onClick={() => setSettingsOpen(true)}>Settings</button>
          <div className="studio-topbar-meta"><span className="studio-status-dot" aria-hidden="true" /> Agent context connected</div>
        </header>
        <ProfilePrimer bootstrap={bootstrap} onProfile={() => setProfileOpen(true)} onBootstrap={(next) => setBootstrap(next)} />
        {selected ? <StudioThread key={selected.id} conversation={selected} seedRun={selected.id === bootstrap.current_conversation?.id ? bootstrap.active_run : null} onRunActivityChange={setAgentRunActive} onRunFinished={handleRunFinished} /> : <div className="studio-no-thread"><h2>Start a conversation</h2><p>Choose New conversation to give the agent a channel context.</p><button type="button" onClick={createConversation}>Open channel desk</button></div>}
      </main>
      <DraftPanel conversationId={selected?.id ?? null} seedDraft={bootstrap.draft} open={draftOpen} onClose={() => setDraftOpen(false)} watchForAgentChanges={agentRunActive} refreshToken={draftRefreshToken} />
    </div>
  );
}

const mount = document.getElementById("studio-react-root");
if (mount) createRoot(mount).render(<StudioApp />);
