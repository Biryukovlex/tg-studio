import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { api, StudioApiError } from "./api";

describe("api session handling", () => {
  const originalFetch = globalThis.fetch;
  const originalLocation = window.location;
  let assignMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    assignMock = vi.fn();
    Object.defineProperty(window, "location", {
      value: { assign: assignMock, href: "" } as unknown as Location,
      writable: true,
      configurable: true,
    });
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    Object.defineProperty(window, "location", {
      value: originalLocation,
      writable: true,
      configurable: true,
    });
    vi.restoreAllMocks();
  });

  it("redirects to /login and throws 401 on redirected response", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      redirected: true,
      status: 200,
      headers: { get: () => "text/html" },
      json: async () => ({}),
    } as unknown as Response);

    await expect(api("/studio/api/conversations")).rejects.toMatchObject({ status: 401 });
    expect(assignMock).toHaveBeenCalledWith("/login");
    try {
      await api("/studio/api/conversations");
    } catch (e) {
      expect((e as StudioApiError).payload.error?.code).toBe("unauthenticated");
    }
  });

  it("redirects and throws 401 on 401 JSON", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      redirected: false,
      status: 401,
      headers: { get: () => "application/json" },
      json: async () => ({ error: { code: "unauthenticated", message: "Sign in", retryable: false } }),
    } as unknown as Response);

    await expect(api("/studio/api/bootstrap")).rejects.toMatchObject({ status: 401 });
    expect(assignMock).toHaveBeenCalledWith("/login");
  });

  it("redirects when content-type is not json", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      redirected: false,
      status: 200,
      headers: { get: () => "text/html; charset=utf-8" },
      json: async () => ({}) ,
    } as unknown as Response);

    await expect(api("/studio/api/setup")).rejects.toMatchObject({ status: 401 });
    expect(assignMock).toHaveBeenCalledWith("/login");
  });

  it("returns payload on successful json response", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      redirected: false,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({ ok: true }),
    } as unknown as Response);

    const data = await api<{ ok: boolean }>("/studio/api/setup");
    expect(data.ok).toBe(true);
    expect(assignMock).not.toHaveBeenCalled();
  });
});
