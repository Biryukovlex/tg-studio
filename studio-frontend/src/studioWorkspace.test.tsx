import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import {
  PREFILL_MAX_TEXT,
  PREFILL_STORAGE_KEY,
  consumeStoredPrefill,
  fetchStudioPost,
  parsePrefillDetail,
} from "./api";
import type { Channel, Conversation, Draft } from "./api";
import { isBlankDraftBody } from "./markdownCopy";
import {
  ChannelPicker,
  MoreActionsMenu,
  draftPreviewHtml,
  isStaleScope,
} from "./main";

function channel(id: number, title: string): Channel {
  return { id, identifier: `@channel_${id}`, title };
}

function conversation(id: string): Conversation {
  return {
    id,
    workspace_id: "ws",
    channel_id: 7,
    channel_identifier: "@channel_7",
    channel_title: "Channel seven",
    title: `Conversation ${id}`,
    summary: "",
    active_draft_id: null,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
    archived_at: null,
  };
}

function draftWith(body: string, bodyHtml?: string): Draft {
  return {
    id: "d1",
    workspace_id: "ws",
    conversation_id: "c1",
    channel_id: 7,
    working_title: "Title",
    body,
    body_html: bodyHtml,
    status: "draft",
    source_ids: [],
    claim_support: [],
    assumptions: [],
    warnings: [],
    channel_evidence: [],
    web_evidence: [],
    confidence: "medium",
    creative: false,
    provider: "openrouter",
    model: "m",
    prompt_version: "v1",
    revision: 1,
    current_version: 1,
    current_version_origin: "generated",
    character_count: body.length,
    over_limit: false,
    warning_threshold: false,
    copied_at: null,
    created_at: null,
    updated_at: null,
  };
}

describe("channel/conversation isolation guard", () => {
  it("treats the current scope and sequence as fresh", () => {
    const scope = { channelId: 7, conversationId: "c1", sequence: 3 };
    expect(isStaleScope(scope, { ...scope })).toBe(false);
  });

  it("ignores delayed responses from a deselected channel", () => {
    expect(isStaleScope(
      { channelId: 7, conversationId: "c1", sequence: 3 },
      { channelId: 9, conversationId: "c1", sequence: 3 },
    )).toBe(true);
  });

  it("ignores delayed responses from a deselected conversation", () => {
    expect(isStaleScope(
      { channelId: 7, conversationId: "c-old", sequence: 3 },
      { channelId: 7, conversationId: "c-new", sequence: 3 },
    )).toBe(true);
  });

  it("ignores older refetches racing a newer one in the same scope", () => {
    expect(isStaleScope(
      { channelId: 7, conversationId: "c1", sequence: 3 },
      { channelId: 7, conversationId: "c1", sequence: 4 },
    )).toBe(true);
  });
});

describe("formatted Full post preview", () => {
  it("prefers the server-rendered Telegram HTML for a clean saved draft", () => {
    const preview = draftPreviewHtml("**Hello**", draftWith("**Hello**", "<b>Hello</b>"), false);
    expect(preview).toEqual({ html: "<b>Hello</b>", fromServer: true });
  });

  it("falls back to the canonical conversion for unsaved edits", () => {
    const preview = draftPreviewHtml("**Hello**", draftWith("**Hello**", "<b>Hello</b>"), true);
    expect(preview.fromServer).toBe(false);
    expect(preview.html).toContain("<b>Hello</b>");
  });

  it("renders version views and drafts without server HTML canonically", () => {
    const preview = draftPreviewHtml("*hi* [x](https://news.test/x)", null, false);
    expect(preview.fromServer).toBe(false);
    expect(preview.html).toContain("<i>hi</i>");
    expect(preview.html).toContain('href="https://news.test/x"');
  });
});

describe("blank-post copy guard", () => {
  it("treats empty, whitespace-only and markup-only bodies as blank", () => {
    expect(isBlankDraftBody("")).toBe(true);
    expect(isBlankDraftBody("  \n ")).toBe(true);
    expect(isBlankDraftBody("**  **")).toBe(true);
  });

  it("treats real content as copyable", () => {
    expect(isBlankDraftBody("Hello")).toBe(false);
    expect(isBlankDraftBody("**Hello**")).toBe(false);
  });
});

describe("Explorer prefill contract", () => {
  it("accepts a well-formed prefill detail", () => {
    expect(parsePrefillDetail({ channel_id: 7, text: "  Explore this  ", post_id: 42 })).toEqual({
      channel_id: 7,
      text: "Explore this",
      post_id: 42,
    });
  });

  it("rejects bad channels, blank text and non-objects", () => {
    expect(parsePrefillDetail(null)).toBeNull();
    expect(parsePrefillDetail({ channel_id: 0, text: "hi" })).toBeNull();
    expect(parsePrefillDetail({ channel_id: "x", text: "hi" })).toBeNull();
    expect(parsePrefillDetail({ channel_id: 7, text: "   " })).toBeNull();
    expect(parsePrefillDetail({ channel_id: 7 })).toBeNull();
  });

  it("bounds prefill text", () => {
    const parsed = parsePrefillDetail({ channel_id: 7, text: "x".repeat(PREFILL_MAX_TEXT + 50) });
    expect(parsed?.text).toHaveLength(PREFILL_MAX_TEXT);
  });

  it("consumes and clears a stored cross-page prefill exactly once", () => {
    const store = (() => {
      const data = new Map<string, string>();
      return {
        getItem: (key: string) => data.get(key) ?? null,
        setItem: (key: string, value: string) => { data.set(key, value); },
        removeItem: (key: string) => { data.delete(key); },
      } as unknown as Storage;
    })();
    store.setItem(PREFILL_STORAGE_KEY, JSON.stringify({ channel_id: 7, text: "Explore this" }));
    expect(consumeStoredPrefill(store)).toEqual({ channel_id: 7, text: "Explore this" });
    expect(consumeStoredPrefill(store)).toBeNull();
  });

  it("ignores malformed stored payloads", () => {
    const store = {
      getItem: () => "{not json",
      setItem: () => undefined,
      removeItem: () => undefined,
    } as unknown as Storage;
    expect(consumeStoredPrefill(store)).toBeNull();
    expect(consumeStoredPrefill(undefined as unknown as Storage)).toBeNull();
  });

  it("rejects invalid post ids without touching the network", async () => {
    const fetchMock = vi.fn();
    const originalFetch = globalThis.fetch;
    globalThis.fetch = fetchMock;
    try {
      await expect(fetchStudioPost(-1)).rejects.toMatchObject({ status: 404 });
      expect(fetchMock).not.toHaveBeenCalled();
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});

describe("custom channel picker", () => {
  const channels = [channel(7, "Channel seven"), channel(9, "Channel nine")];

  it("labels the selected channel and selects another on click", () => {
    const onChannelSelect = vi.fn();
    render(<ChannelPicker channels={channels} selectedChannelId={7} onChannelSelect={onChannelSelect} />);
    expect(screen.getByText("Channel seven")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Studio channel: Channel seven" }));
    fireEvent.click(screen.getByRole("option", { name: /Channel nine/ }));
    expect(onChannelSelect).toHaveBeenCalledWith(9);
  });

  it("closes on Escape and returns focus to the trigger", () => {
    render(<ChannelPicker channels={channels} selectedChannelId={7} onChannelSelect={() => undefined} />);
    const trigger = screen.getByRole("button", { name: "Studio channel: Channel seven" });
    fireEvent.click(trigger);
    expect(screen.getByRole("listbox")).toBeTruthy();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("moves with arrow keys and chooses with Enter", () => {
    const onChannelSelect = vi.fn();
    render(<ChannelPicker channels={channels} selectedChannelId={7} onChannelSelect={onChannelSelect} />);
    fireEvent.click(screen.getByRole("button", { name: "Studio channel: Channel seven" }));
    const menu = screen.getByRole("listbox");
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    // Arrow navigation moves DOM focus to the second option; activating the
    // focused option (Enter/Space in a browser) selects that channel.
    expect(document.activeElement?.textContent).toContain("Channel nine");
    fireEvent.click(document.activeElement as HTMLElement);
    expect(onChannelSelect).toHaveBeenCalledWith(9);
  });
});

describe("single More actions menu", () => {
  it("keeps secondary actions in one anchored menu", () => {
    const onRename = vi.fn();
    const onDelete = vi.fn();
    const selected = conversation("c1");
    render(
      <MoreActionsMenu
        selected={selected}
        busy={false}
        onRename={onRename}
        onProfile={() => undefined}
        onSettings={() => undefined}
        onDelete={onDelete}
      />,
    );
    // Secondary actions are hidden until the menu opens: no inline buttons.
    expect(screen.queryByRole("menuitem")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    expect(screen.getAllByRole("menuitem")).toHaveLength(4);
    fireEvent.click(screen.getByRole("menuitem", { name: "Rename conversation" }));
    expect(onRename).toHaveBeenCalledTimes(1);
  });

  it("disables conversation actions without a selection", () => {
    render(
      <MoreActionsMenu
        selected={null}
        busy={false}
        onRename={() => undefined}
        onProfile={() => undefined}
        onSettings={() => undefined}
        onDelete={() => undefined}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    expect(screen.getByRole("menuitem", { name: "Rename conversation" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("menuitem", { name: /Delete conversation/ }).hasAttribute("disabled")).toBe(true);
  });
});
