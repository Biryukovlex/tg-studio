import { describe, expect, it } from "vitest";
import { StudioApiError } from "./api";
import { isActiveRun, isConversationActiveRunError } from "./main";

describe("Studio shell run and delete helpers", () => {
  it("recognizes an active-run delete conflict", () => {
    expect(isConversationActiveRunError(new StudioApiError(409, {
      error: { code: "conversation_active_run", message: "Stop the active run before deleting this conversation." },
    }))).toBe(true);
    expect(isConversationActiveRunError(new StudioApiError(409, {
      error: { code: "other_conflict", message: "Try again." },
    }))).toBe(false);
  });

  it("keeps the stop control scoped to queued and running runs", () => {
    expect(isActiveRun({ status: "queued" } as never)).toBe(true);
    expect(isActiveRun({ status: "running" } as never)).toBe(true);
    expect(isActiveRun({ status: "cancelled" } as never)).toBe(false);
    expect(isActiveRun(null)).toBe(false);
  });
});
