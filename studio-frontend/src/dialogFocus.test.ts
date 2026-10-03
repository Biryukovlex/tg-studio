import { describe, expect, it } from "vitest";
import { focusableIn, trapTabInDialog } from "./dialogFocus";

function dialogWith(...labels: string[]): HTMLDialogElement {
  const dialog = document.createElement("dialog");
  dialog.setAttribute("open", "");
  labels.forEach((label) => {
    const button = document.createElement("button");
    button.textContent = label;
    dialog.append(button);
  });
  document.body.append(dialog);
  return dialog;
}

function tabKey(shift = false): KeyboardEvent {
  return new KeyboardEvent("keydown", { key: "Tab", shiftKey: shift, bubbles: true, cancelable: true });
}

describe("trapTabInDialog", () => {
  it("cycles forward from the last control to the first", () => {
    const dialog = dialogWith("One", "Two");
    const [first, last] = [...dialog.querySelectorAll("button")];
    last.focus();
    const event = tabKey();
    expect(trapTabInDialog(dialog, event)).toBe(true);
    expect(event.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(first);
    dialog.remove();
  });

  it("cycles backward from the first control to the last", () => {
    const dialog = dialogWith("One", "Two");
    const buttons = [...dialog.querySelectorAll("button")];
    buttons[0].focus();
    const event = tabKey(true);
    expect(trapTabInDialog(dialog, event)).toBe(true);
    expect(document.activeElement).toBe(buttons[buttons.length - 1]);
    dialog.remove();
  });

  it("leaves middle focus alone and ignores non-Tab keys", () => {
    const dialog = dialogWith("One", "Two", "Three");
    const buttons = [...dialog.querySelectorAll("button")];
    buttons[1].focus();
    expect(trapTabInDialog(dialog, tabKey())).toBe(false);
    expect(document.activeElement).toBe(buttons[1]);
    expect(trapTabInDialog(dialog, new KeyboardEvent("keydown", { key: "Escape" }))).toBe(false);
    dialog.remove();
  });

  it("skips disabled controls when listing focusables", () => {
    const dialog = dialogWith("One", "Two");
    (dialog.querySelectorAll("button")[1] as HTMLButtonElement).disabled = true;
    expect(focusableIn(dialog)).toHaveLength(1);
    dialog.remove();
  });
});
