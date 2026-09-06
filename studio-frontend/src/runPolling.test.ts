import { describe, it, expect } from "vitest";
import { isTerminalPollStatus, nextPollDelay, shouldStopPollingAfterErrors } from "./runPolling";

describe("runPolling helper", () => {
  it("stops after 404, 401, 403", () => {
    expect(isTerminalPollStatus(404)).toBe(true);
    expect(isTerminalPollStatus(401)).toBe(true);
    expect(isTerminalPollStatus(403)).toBe(true);
    expect(isTerminalPollStatus(500)).toBe(false);
    expect(isTerminalPollStatus(200)).toBe(false);
  });

  it("backs off exponentially from 1.5s to 15s", () => {
    expect(nextPollDelay(1)).toBe(1500);
    const d2 = nextPollDelay(2);
    const d3 = nextPollDelay(3);
    expect(d2).toBeGreaterThan(1500);
    expect(d3).toBeGreaterThan(d2);
    expect(nextPollDelay(10)).toBeLessThanOrEqual(15000);
    expect(nextPollDelay(20)).toBe(15000);
  });

  it("stops after 10 consecutive errors", () => {
    expect(shouldStopPollingAfterErrors(9)).toBe(false);
    expect(shouldStopPollingAfterErrors(10)).toBe(true);
    expect(shouldStopPollingAfterErrors(11)).toBe(true);
  });

  it("poll loop would stop on 404 and not retry forever", () => {
    // Simulate poll error handling: 404 is terminal, should clear run and finish
    const status = 404;
    const terminal = isTerminalPollStatus(status);
    expect(terminal).toBe(true);
    // backs off after non-terminal errors
    let delay = nextPollDelay(1);
    expect(delay).toBe(1500);
    delay = nextPollDelay(5);
    expect(delay).toBeGreaterThan(1500);
    expect(delay).toBeLessThanOrEqual(15000);
  });
});
