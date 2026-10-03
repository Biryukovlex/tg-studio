import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AgentActivity, focusComposer, isSetupBlockerCode, mergeRunEvents } from "./main";
import type { RunEvent, RunSummary } from "./api";

function event(sequence: number, type = "TOOL_CALL_RESULT"): RunEvent {
  return {
    id: sequence,
    sequence,
    event_type: type,
    safe_payload: { tool_name: "search_web" },
    created_at: null,
  };
}

function run(status: RunSummary["status"]): RunSummary {
  return {
    id: "run-1",
    conversation_id: "c1",
    status,
    stage: status,
    provider: "test",
    requested_model: "m",
    actual_model: null,
    usage: { requests: 0, tool_calls: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0 },
    error_code: null,
    error_message: null,
    created_at: new Date(Date.now() - 5000).toISOString(),
    started_at: new Date(Date.now() - 5000).toISOString(),
    finished_at: null,
    duration_ms: null,
  };
}

describe("mergeRunEvents", () => {
  it("suppresses duplicates by sequence across overlapping windows", () => {
    const merged = mergeRunEvents([event(1), event(2)], [event(2), event(3)]);
    expect(merged.map((e) => e.sequence)).toEqual([1, 2, 3]);
  });

  it("reorders delayed and out-of-order delivery", () => {
    const merged = mergeRunEvents([event(3)], [event(1), event(2)]);
    expect(merged.map((e) => e.sequence)).toEqual([1, 2, 3]);
  });

  it("keeps only the newest events and ignores bad sequences", () => {
    const many = Array.from({ length: 30 }, (_, i) => event(i + 1));
    const merged = mergeRunEvents(many, [{ ...event(31), sequence: NaN }]);
    expect(merged).toHaveLength(24);
    expect(merged[0].sequence).toBe(7);
    expect(merged.at(-1)?.sequence).toBe(30);
  });
});

describe("isSetupBlockerCode", () => {
  it.each([
    "provider_consent_required",
    "studio_not_ready",
    "provider_not_configured",
    "model_not_configured",
    "setup_required",
  ])("routes %s to Settings", (code) => {
    expect(isSetupBlockerCode(code)).toBe(true);
  });

  it.each(["agent_failed", "model_error", "timeout", "search_failed", null, undefined, ""])(
    "keeps %s on retry without setup detour",
    (code) => {
      expect(isSetupBlockerCode(code)).toBe(false);
    },
  );
});

describe("focusComposer", () => {
  it("focuses the message composer when present", () => {
    render(
      <div className="studio-composer">
        <input aria-label="Message the Studio agent" />
      </div>,
    );
    expect(focusComposer()).toBe(true);
    expect((document.activeElement as HTMLInputElement)?.getAttribute("aria-label")).toBe(
      "Message the Studio agent",
    );
  });

  it("reports failure without a composer", () => {
    render(<div />);
    expect(focusComposer()).toBe(false);
  });
});

describe("AgentActivity truthfulness", () => {
  it("renders inline thinking with elapsed time and Stop for a live run", () => {
    render(<AgentActivity run={run("running")} events={[event(1, "TOOL_CALL_START")]} onStopRun={async () => {}} />);
    expect(screen.getByText(/Searching the web|Thinking/)).toBeDefined();
    expect(screen.getByText("Stop run")).toBeDefined();
    expect(screen.queryByText(/%/)).toBeNull();
    expect(screen.queryByText(/Step \d/)).toBeNull();
  });

  it("renders nothing once the run settles", () => {
    const { container } = render(<AgentActivity run={run("succeeded")} events={[]} onStopRun={async () => {}} />);
    expect(container.textContent).toBe("");
    const { container: empty } = render(<AgentActivity run={null} events={[]} onStopRun={async () => {}} />);
    expect(empty.textContent).toBe("");
  });

  it("never reports cancellation locally: Stop stays pending until the server settles", async () => {
    let rejectStop!: (reason?: unknown) => void;
    const onStopRun = vi.fn(() => new Promise<void>((_, reject) => {
      rejectStop = reject;
    }));
    render(<AgentActivity run={run("running")} events={[]} onStopRun={onStopRun} />);
    fireEvent.click(screen.getByRole("button", { name: "Stop run" }));
    expect(onStopRun).toHaveBeenCalledWith("run-1");
    expect(screen.getByRole("button", { name: "Stopping…" })).toBeDefined();
    // A failed cancel returns to Stop; only a server status change settles it.
    rejectStop(new Error("network down"));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Stop run" })).toBeDefined();
    });
  });
});
