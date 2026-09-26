import { describe, expect, it } from "vitest";
import { describeSearchOutcome } from "./api";

describe("describeSearchOutcome", () => {
  it("stays silent for healthy research", () => {
    expect(describeSearchOutcome({ degraded: false, search_outcome: "healthy", failed_engines: [] })).toBeNull();
    expect(describeSearchOutcome({ degraded: false })).toBeNull();
  });

  it("names failed engines for partial batches but keeps results usable", () => {
    const notice = describeSearchOutcome({ degraded: true, search_outcome: "partial", failed_engines: ["bing"] });
    expect(notice).toContain("partial");
    expect(notice).toContain("bing");
    expect(notice).toContain("still usable");
  });

  it("reports unavailable batches with the failing engines", () => {
    const notice = describeSearchOutcome({ degraded: true, search_outcome: "unavailable", failed_engines: ["google", "bing"] });
    expect(notice).toContain("unavailable");
    expect(notice).toContain("google");
  });

  it("distinguishes an intentional empty result from failure", () => {
    const notice = describeSearchOutcome({ degraded: false, search_outcome: "empty", failed_engines: [] });
    expect(notice).toContain("no results");
    expect(notice).not.toContain("unavailable");
  });

  it("keeps the legacy degraded message when no outcome is known", () => {
    expect(describeSearchOutcome({ degraded: true })).toBe("Research was degraded for this run.");
  });
});
