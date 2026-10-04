import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  ConversationTitle,
  MyChannels,
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
  it("contains System Prompt and deletion without duplicate profile or rename", () => {
    const onSettings = vi.fn();
    render(<MoreActionsMenu selected={conversation("c1")} busy={false} onSettings={onSettings} onDelete={vi.fn()} />);
    expect(screen.queryByRole("menuitem")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    expect(screen.getAllByRole("menuitem")).toHaveLength(2);
    expect(screen.queryByRole("menuitem", { name: /Profile|Rename/ })).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: /System Prompt/ }));
    expect(onSettings).toHaveBeenCalledTimes(1);
  });
  it("disables deletion without a selected conversation", () => {
    render(<MoreActionsMenu selected={null} busy={false} onSettings={vi.fn()} onDelete={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    expect(screen.getByRole("menuitem", { name: /Delete conversation/ }).hasAttribute("disabled")).toBe(true);
  });
});

afterEach(() => vi.unstubAllGlobals());

describe("inline conversation title", () => {
  it.each(["Enter", "blur"])("saves the title once on %s", async (action) => {
    const renamed = { ...conversation("c1"), title: "New title" };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({conversation: renamed}), {status:200,headers:{"content-type":"application/json"}}));
    vi.stubGlobal("fetch", fetchMock);
    const onRenamed = vi.fn();
    render(<ConversationTitle conversation={conversation("c1")} onRenamed={onRenamed} />);
    fireEvent.click(screen.getByRole("button", {name:"Rename conversation"}));
    const input = screen.getByRole("textbox", {name:"Conversation title"});
    expect(document.activeElement).toBe(input);
    fireEvent.change(input, {target:{value:"  New title  "}});
    if (action === "Enter") fireEvent.keyDown(input, {key:"Enter"});
    fireEvent.blur(input);
    await waitFor(() => expect(onRenamed).toHaveBeenCalledWith(renamed));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({title:"New title"});
  });
  it("does not switch back to a previous conversation when its save returns late", async () => {
    let resolve!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => new Promise<Response>((done) => {resolve = done;})));
    const onRenamed = vi.fn();
    const view = render(<ConversationTitle conversation={conversation("c1")} onRenamed={onRenamed} />);
    fireEvent.click(screen.getByRole("button", {name:"Rename conversation"}));
    fireEvent.change(screen.getByRole("textbox"), {target:{value:"Late result"}});
    fireEvent.keyDown(screen.getByRole("textbox"), {key:"Enter"});
    view.rerender(<ConversationTitle conversation={conversation("c2")} onRenamed={onRenamed} />);
    await act(async () => resolve(new Response(JSON.stringify({conversation:{...conversation("c1"),title:"Late result"}}), {headers:{"content-type":"application/json"}})));
    expect(onRenamed).not.toHaveBeenCalled();
    expect(screen.getByRole("button", {name:"Rename conversation"}).textContent).toBe("Conversation c2");
  });
  it("cancels on Escape without persisting the edited text", () => {
    const fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock);
    render(<ConversationTitle conversation={conversation("c1")} onRenamed={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", {name:"Rename conversation"}));
    const input = screen.getByRole("textbox");
    fireEvent.change(input, {target:{value:"Cancelled"}});
    fireEvent.keyDown(input, {key:"Escape"});
    expect(screen.getByRole("button", {name:"Rename conversation"}).textContent).toBe("Conversation c1");
    expect(fetchMock).not.toHaveBeenCalled();
  });
  it("keeps the edited value when saving fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({error:{message:"Try again"}}), {status:500,headers:{"content-type":"application/json"}})));
    render(<ConversationTitle conversation={conversation("c1")} onRenamed={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", {name:"Rename conversation"}));
    fireEvent.change(screen.getByRole("textbox"), {target:{value:"Keep me"}});
    fireEvent.keyDown(screen.getByRole("textbox"), {key:"Enter"});
    await screen.findByRole("alert");
    expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe("Keep me");
  });
});

describe("conversation reference channels", () => {
  it("requires permission, persists a selection and allows removal", async () => {
    const ref = {...channel(9,"Other channel"), summary:"Useful topics", selected:false, needs_renewal:false};
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({references:[ref]}), {headers:{"content-type":"application/json"}}))
      .mockResolvedValueOnce(new Response(JSON.stringify({references:[{...ref,selected:true}]}), {headers:{"content-type":"application/json"}}))
      .mockResolvedValueOnce(new Response(JSON.stringify({references:[ref]}), {headers:{"content-type":"application/json"}}));
    vi.stubGlobal("fetch",fetchMock);
    render(<MyChannels channels={[channel(7,"Main"),ref]} selectedChannelId={7} conversationId="c1" />);
    fireEvent.click(screen.getByRole("button",{name:/References/}));
    const add = await screen.findByRole("button",{name:"Use in this conversation"});
    expect(add.hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(add);
    const remove = await screen.findByRole("button",{name:"Remove from context"});
    expect(screen.getByRole("status").textContent).toBe("Connected to this conversation");
    expect(fetchMock.mock.calls[1][0]).toBe("/studio/api/conversations/c1/references/9");
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({enabled:true,permission:true});
    fireEvent.click(remove);
    await screen.findByRole("button",{name:"Use in this conversation"});
    expect(JSON.parse(fetchMock.mock.calls[2][1].body).enabled).toBe(false);
  });
});


describe("reference-channel availability", () => {
  it("clears a stale busy hint using the conversation's saved status", async () => {
    const ref = {...channel(9,"Other channel"), summary:"Useful topics", selected:false, needs_renewal:false};
    const fetchMock = vi.fn((url: string) => Promise.resolve(new Response(JSON.stringify(
      url.endsWith("active-run") ? {run:null} : {references:[ref]},
    ), {headers:{"content-type":"application/json"}})));
    vi.stubGlobal("fetch",fetchMock);
    render(<MyChannels channels={[channel(7,"Main"),ref]} selectedChannelId={7} conversationId="c1" busy />);
    fireEvent.click(screen.getByRole("button",{name:/References/}));
    const add = await screen.findByRole("button",{name:"Use in this conversation"});
    fireEvent.click(screen.getByRole("checkbox"));
    await waitFor(() => expect(add.hasAttribute("disabled")).toBe(false));
    expect(fetchMock.mock.calls.some(([url]) => url.endsWith("active-run"))).toBe(true);
  });
  it("explains a genuine active-run restriction and reenables after completion", async () => {
    const ref = {...channel(9,"Other channel"), summary:"Useful topics", selected:false, needs_renewal:false};
    vi.stubGlobal("fetch",vi.fn((url: string) => Promise.resolve(new Response(JSON.stringify(
      url.endsWith("active-run") ? {run:{status:"running"}} : {references:[ref]},
    ), {headers:{"content-type":"application/json"}}))));
    const props = {channels:[channel(7,"Main"),ref], selectedChannelId:7, conversationId:"c1"};
    const {rerender}=render(<MyChannels {...props} busy />);
    fireEvent.click(screen.getByRole("button",{name:/References/}));
    const add=await screen.findByRole("button",{name:"Use in this conversation"});
    fireEvent.click(screen.getByRole("checkbox"));
    expect(add.hasAttribute("disabled")).toBe(true);
    expect(screen.getByText(/Wait for the current reply/)).not.toBeNull();
    rerender(<MyChannels {...props} busy={false} />);
    await waitFor(() => expect(add.hasAttribute("disabled")).toBe(false));
  });
});
