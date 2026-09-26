import { describe, expect, it } from "vitest";
import { StudioApiError } from "./api";
import { claimDisplayItems, draftCounterState, draftSaveErrorMessage } from "./main";
import type { Draft } from "./api";

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

describe("DraftPanel claim support display", () => {
  const links = new Map([
    ["s-a", { url: "https://news.test/a", title: "Article A" }],
  ]);

  function draftWith(claims: Draft["claim_support"]): Draft {
    return {
      claim_support: claims,
    } as Draft;
  }

  it("marks verified claims and resolves their source links", () => {
    const items = claimDisplayItems(
      draftWith([{
        claim: "Company A raised funds.",
        source_ids: ["s-a"],
        passage: "raised funds",
        verified: true,
      }]),
      links,
    );
    expect(items).toHaveLength(1);
    expect(items[0].verified).toBe(true);
    expect(items[0].passage).toBe("raised funds");
    expect(items[0].links).toEqual([{ id: "s-a", url: "https://news.test/a", title: "Article A" }]);
  });

  it("flags unverified claims and drops unknown source links", () => {
    const items = claimDisplayItems(
      draftWith([
        { claim: "Edited claim.", source_ids: ["s-a"], verified: false },
        { claim: "Ghost claim.", source_ids: ["ghost"], verified: false },
      ]),
      links,
    );
    expect(items.map((item) => item.verified)).toEqual([false, false]);
    expect(items[0].links).toHaveLength(1);
    expect(items[1].links).toEqual([]);
  });

  it("renders nothing without claims", () => {
    expect(claimDisplayItems(draftWith([]), links)).toEqual([]);
    expect(claimDisplayItems(null, links)).toEqual([]);
  });
});
