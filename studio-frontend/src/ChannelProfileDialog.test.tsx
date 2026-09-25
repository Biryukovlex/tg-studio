import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ChannelProfileDialog, {
  PreviewMarkdown,
  extractConflictProfile,
  isProfileConflictError,
  isProfileDirty,
  needsBuildConfirm,
} from "./ChannelProfileDialog";
import { StudioApiError } from "./api";

describe("ChannelProfileDialog dirty tracking", () => {
  it("starts clean and marks dirty on edit", () => {
    const initial = { topics: "A", editorial: "B", style: "C", version: 3 };
    expect(isProfileDirty(initial, { topics: "A", editorial: "B", style: "C" })).toBe(false);
    expect(isProfileDirty(initial, { topics: "A2", editorial: "B", style: "C" })).toBe(true);
    expect(isProfileDirty(initial, { topics: "A", editorial: "B2", style: "C" })).toBe(true);
    expect(isProfileDirty(initial, { topics: "A", editorial: "B", style: "C2" })).toBe(true);
  });

  it("treats any text as dirty before the first load", () => {
    expect(isProfileDirty(null, { topics: "", editorial: "", style: "" })).toBe(false);
    expect(isProfileDirty(null, { topics: "x", editorial: "", style: "" })).toBe(true);
  });
});

describe("ChannelProfileDialog build-confirm guard", () => {
  it("asks only when a field is non-empty", () => {
    expect(needsBuildConfirm("", "", "")).toBe(false);
    expect(needsBuildConfirm("  ", "\n", "")).toBe(false);
    expect(needsBuildConfirm("topic", "", "")).toBe(true);
    expect(needsBuildConfirm("", "rule", "")).toBe(true);
    expect(needsBuildConfirm("", "", "style")).toBe(true);
  });
});

describe("ChannelProfileDialog 409 handling", () => {
  it("detects conflicts and extracts the server profile", () => {
    const server = {
      channel_id: 1,
      version: 4,
      topics_text: "their",
      editorial_text: "",
      style_text: "",
      updated_at: null,
      built_at: null,
      built_from_posts: 0,
    };
    const conflict = new StudioApiError(409, {
      error: { code: "profile_conflict", message: "conflict" },
      // @ts-expect-error server_profile is an extra payload field
      server_profile: server,
    });
    expect(isProfileConflictError(conflict)).toBe(true);
    expect(extractConflictProfile(conflict)).toEqual(server);
    expect(isProfileConflictError(new StudioApiError(422, { error: { message: "bad" } }))).toBe(false);
    expect(extractConflictProfile(new StudioApiError(422, { error: { message: "bad" } }))).toBeNull();
    expect(isProfileConflictError(new Error("boom"))).toBe(false);
  });
});

describe("ChannelProfileDialog preview", () => {
  it("renders bold and links, hides images", () => {
    const { container } = render(
      <PreviewMarkdown text={"**bold title**\n[x](https://x)\n![i](https://x)\n> quote"} />,
    );
    expect(container.querySelector("strong")?.textContent).toBe("bold title");
    const link = container.querySelector("a");
    expect(link?.getAttribute("href")).toBe("https://x");
    expect(link?.getAttribute("target")).toBe("_blank");
    expect(link?.getAttribute("rel")).toBe("noopener noreferrer");
    expect(container.textContent).not.toContain("![i]");
    expect(container.querySelector("blockquote")).not.toBeNull();
  });
});

describe("ChannelProfileDialog Edit/Preview toggle", () => {
  beforeEach(() => {
    // jsdom does not implement showModal; the dialog opens imperatively.
    // A closed <dialog> is inert, so mark it open for role queries.
    HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) {
      this.setAttribute("open", "");
    });
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true,
      status: 200,
      redirected: false,
      headers: { get: () => "application/json" },
      json: async () => ({
        profile: {
          channel_id: 7,
          version: 2,
          topics_text: "Topic **bold**",
          editorial_text: "Rule",
          style_text: "Style [x](https://x)",
          updated_at: null,
          built_at: null,
          built_from_posts: 0,
        },
        can_build: true,
        build_blockers: [],
        channel_id: 7,
      }),
    }) as unknown as Response));
  });

  it("preserves unsaved text when toggling modes", async () => {
    render(<ChannelProfileDialog channelId={7} onClose={() => {}} />);
    const topics = (await screen.findByLabelText("Topics")) as HTMLTextAreaElement;
    fireEvent.change(topics, { target: { value: "Edited **topic**" } });
    expect(screen.getByText("Unsaved changes")).toBeDefined();

    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() => {
      expect(screen.getByText("Edited").parentElement?.querySelector("strong")?.textContent).toBe("topic");
    });

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const back = screen.getByLabelText("Topics") as HTMLTextAreaElement;
    expect(back.value).toBe("Edited **topic**");
    expect(screen.getByText("Unsaved changes")).toBeDefined();
  });

  it("asks for confirmation before replacing non-empty fields", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<ChannelProfileDialog channelId={7} onClose={() => {}} />);
    await screen.findByLabelText("Topics");
    fireEvent.click(screen.getByRole("button", { name: "Build from posts" }));
    expect(confirm).toHaveBeenCalledWith(
      "Replace the text in all three fields with a fresh analysis? Nothing is saved until you press Save.",
    );
    // Declining leaves the loaded text untouched.
    expect((screen.getByLabelText("Topics") as HTMLTextAreaElement).value).toBe("Topic **bold**");
    confirm.mockRestore();
  });

  it("shows both conflict actions on a 409 save", async () => {
    let calls = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      calls += 1;
      if (calls === 1) {
        return {
          ok: true,
          status: 200,
          redirected: false,
          headers: { get: () => "application/json" },
          json: async () => ({
            profile: {
              channel_id: 7,
              version: 2,
              topics_text: "mine",
              editorial_text: "",
              style_text: "",
              updated_at: null,
              built_at: null,
              built_from_posts: 0,
            },
            can_build: true,
            build_blockers: [],
            channel_id: 7,
          }),
        } as unknown as Response;
      }
      return {
        ok: false,
        status: 409,
        redirected: false,
        headers: { get: () => "application/json" },
        json: async () => ({
          error: { code: "profile_conflict", message: "conflict" },
          server_profile: {
            channel_id: 7,
            version: 3,
            topics_text: "theirs",
            editorial_text: "",
            style_text: "",
            updated_at: null,
            built_at: null,
            built_from_posts: 0,
          },
        }),
      } as unknown as Response;
    }));
    render(<ChannelProfileDialog channelId={7} onClose={() => {}} />);
    const topics = (await screen.findByLabelText("Topics")) as HTMLTextAreaElement;
    fireEvent.change(topics, { target: { value: "mine edited" } });
    fireEvent.click(screen.getByRole("button", { name: /Save as v3/ }));
    await screen.findByText(/Someone saved v3 while you were editing/);
    expect(screen.getByRole("button", { name: "Load their version" })).toBeDefined();
    expect(screen.getByRole("button", { name: "Save mine as v4" })).toBeDefined();
  });
});
