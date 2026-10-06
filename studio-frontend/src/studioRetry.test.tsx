import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StudioThread } from "./main";
import type { Conversation, RunSummary } from "./api";

const conversation = { id: "c-retry", channel_id: 1, title: "Retry test" } as Conversation;
const failed = { id: "failed-run", conversation_id: conversation.id, status: "failed", stage: "failed", provider: "test", requested_model: "test", error_code: "agent_failed", error_message: "Agent failed", usage: {}, created_at: null, started_at: null, finished_at: null, duration_ms: 12 } as RunSummary;
const messages = [
  { id: 1, role: "user", content: "Inspect this channel", metadata: {} },
  { id: 2, role: "assistant", content: "Old failure", metadata: { run_failed: true } },
];
afterEach(() => vi.unstubAllGlobals());

function setup() {
  HTMLElement.prototype.scrollTo = vi.fn();
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  const requests: Record<string, unknown>[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string | URL | Request, options?: RequestInit) => {
    const path = String(url);
    if (path === "/studio/api/agent") {
      const input = JSON.parse(String(options?.body));
      requests.push(input);
      const events = [
        { type: "RUN_STARTED", threadId: conversation.id, runId: input.runId },
        { type: "TEXT_MESSAGE_START", messageId: "answer", role: "assistant" },
        { type: "TEXT_MESSAGE_CONTENT", messageId: "answer", delta: "Retried answer" },
        { type: "TEXT_MESSAGE_END", messageId: "answer" },
        { type: "RUN_FINISHED", threadId: conversation.id, runId: input.runId },
      ];
      return new Response(events.map(event => `data: ${JSON.stringify(event)}\n\n`).join(""), { headers: { "Content-Type": "text/event-stream" } });
    }
    if (path.includes("/messages")) return Response.json({ messages });
    if (path.includes("/events")) return Response.json({ run: failed, events: [] });
    return Response.json({ run: null });
  }));
  render(<StudioThread conversation={conversation} seedRun={failed} consent={{ accepted: true } as never} pendingPrefill={null} onPrefillResult={() => {}} onStopRun={async () => {}} onRunActivityChange={() => {}} onRunFinished={() => {}} />);
  return requests;
}

describe("Try again", () => {
  it("actually starts a fresh run of the stored request and forwards the failed run ID", async () => {
    const requests = setup();
    await waitFor(() => expect(screen.getByRole("button", { name: "Try again" }).hasAttribute("disabled")).toBe(false));
    const composer = screen.getByRole("textbox", { name: "Message the Studio agent" });
    fireEvent.change(composer, { target: { value: "Unsent draft message" } });
    const retryButton = screen.getByRole("button", { name: "Try again" });
    await act(async () => {
      fireEvent.click(retryButton);
      fireEvent.click(retryButton);
    });
    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0].forwardedProps).toEqual({ runConfig: { retryRunId: "failed-run" } });
    expect(requests[0].messages).toEqual([{ id: "persisted-1", role: "user", content: "Inspect this channel" }]);
    expect(requests[0].runId).not.toBe("failed-run");
    expect((composer as HTMLTextAreaElement).value).toBe("Unsent draft message");
  });
});
