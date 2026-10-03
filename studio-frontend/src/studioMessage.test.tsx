import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
const state = vi.hoisted(() => ({ message: { role: "assistant", content: [] as Array<{ type: string; text?: string }> } }));
vi.mock("@assistant-ui/react", async (importOriginal) => ({
  ...await importOriginal<object>(),
  useAuiState: (select: (value: typeof state) => unknown) => select(state),
  MessagePrimitive: {
    Root: ({ children, ...props }: React.PropsWithChildren<Record<string, unknown>>) => <div {...props}>{children}</div>,
    If: ({ children }: React.PropsWithChildren) => <>{children}</>,
    Parts: () => <span>Response content</span>,
  },
}));
import { StudioMessage } from "./main";
afterEach(cleanup);
describe("assistant response bubbles", () => {
  it("mounts the bubble when a pending response starts streaming", () => {
    state.message = { role: "assistant", content: [] };
    const { container, rerender } = render(<StudioMessage />);
    expect(container.childElementCount).toBe(0);
    state.message = { role: "assistant", content: [{ type: "text", text: "First words" }] };
    rerender(<StudioMessage />);
    expect(container.querySelector('[data-role="assistant"]')).not.toBeNull();
  });
  it.each([[], [{ type: "text", text: "" }], [{ type: "text", text: " \n " }]].map(content => ({ content })))("omits the bubble until visible content arrives: %j", ({ content }) => {
    state.message = { role: "assistant", content };
    const { container } = render(<StudioMessage />);
    expect(container.childElementCount).toBe(0);
  });
  it.each([[{ type: "text", text: "Here is the answer." }], [{ type: "tool-call" }]].map(content => ({ content })))("shows actual text and tool activity: %j", ({ content }) => {
    state.message = { role: "assistant", content };
    render(<StudioMessage />);
    expect(screen.getAllByText("Response content").length).toBeGreaterThan(0);
  });
});
