import { describe, expect, it } from "vitest";
import { StudioApiError } from "./api";
import { draftCounterState, draftSaveErrorMessage } from "./main";

describe("DraftPanel state helpers", () => {
  it("uses the displayed version for the counter and copy limit", () => {
    expect(draftCounterState("short", 100)).toEqual({ count: 100, overLimit: false, warning: false });
    expect(draftCounterState("short", 5000)).toEqual({ count: 5000, overLimit: true, warning: true });
  });

  it("surfaces the server's validation message", () => {
    const error = new StudioApiError(422, { error: { message: "Headings are not allowed in the draft body." } });
    expect(draftSaveErrorMessage(error)).toBe("Headings are not allowed in the draft body.");
  });
});
