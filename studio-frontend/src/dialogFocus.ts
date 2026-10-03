import { useEffect, type RefObject } from "react";

/**
 * Modal isolation for native dialogs (T47). Tab and Shift+Tab cycle inside
 * the open dialog instead of leaking to the page behind it. Escape and
 * focus return stay with each dialog's own close handling.
 */
export const FOCUSABLE_SELECTOR =
  "[autofocus], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])";

export function focusableIn(container: ParentNode): HTMLElement[] {
  // offsetParent is always null in jsdom, so hidden subtrees are detected
  // through the hidden attribute instead; disabled controls are already
  // excluded by the selector.
  return [...container.querySelectorAll(FOCUSABLE_SELECTOR)].filter(
    (node): node is HTMLElement =>
      node instanceof HTMLElement && node.closest("[hidden]") === null,
  );
}

/** Move Tab focus within `dialog`; returns true when the key was trapped. */
export function trapTabInDialog(dialog: HTMLDialogElement, event: KeyboardEvent): boolean {
  if (event.key !== "Tab" || !dialog.open) return false;
  const items = focusableIn(dialog);
  if (items.length === 0) {
    event.preventDefault();
    return true;
  }
  const first = items[0];
  const last = items[items.length - 1];
  const active = document.activeElement;
  if (event.shiftKey && (active === first || !dialog.contains(active))) {
    event.preventDefault();
    last.focus();
    return true;
  }
  if (!event.shiftKey && (active === last || !dialog.contains(active))) {
    event.preventDefault();
    first.focus();
    return true;
  }
  return false;
}

/** Attach the Tab loop to an open dialog for as long as it stays mounted. */
export function useDialogFocusTrap(ref: RefObject<HTMLDialogElement | null>): void {
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    const onKey = (event: KeyboardEvent) => trapTabInDialog(dialog, event);
    dialog.addEventListener("keydown", onKey);
    return () => dialog.removeEventListener("keydown", onKey);
  }, [ref]);
}
