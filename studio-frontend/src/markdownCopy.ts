// Markdown dialect helpers for the draft body (spec §1.1): **bold**, *italic*,
// ~~strike~~, `code`, [text](https://url), > quote, single newlines as line
// breaks. Headings, images and HTML are not part of the dialect and are
// reduced to their text.

const TAG_PATTERN = /<[a-zA-Z\/][^>]*>/g;

function isHttpUrl(url: string): string | null {
  const trimmed = url.trim();
  const lowered = trimmed.toLowerCase();
  return lowered.startsWith("http://") || lowered.startsWith("https://") ? trimmed : null;
}

export function plainFromMarkdown(text: string): string {
  let t = text.replace(/`([^`]+)`/g, "$1");
  t = t.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1");
  t = t.replace(/^\s*#{1,6}\s+/gm, "");
  t = t.replace(TAG_PATTERN, "");
  t = t.replace(/\*\*([^*]+)\*\*/g, "$1");
  t = t.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, "$1");
  t = t.replace(/~~([^~]+)~~/g, "$1");
  t = t.replace(/\[([^\]]+)\]\(((?:[^()\s]|\([^()\s]*\))+)\)/g, (_m, label: string, url: string) => {
    const safe = isHttpUrl(url);
    if (!safe) return label;
    return label.trim() === safe ? label : `${label} (${safe})`;
  });
  return t.split("\n").map((line) => line.replace(/^\s*>\s?/, "")).join("\n");
}

function escapeHtml(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function inlineHtml(line: string): string {
  // Strip markup outside the dialect, then escape, then turn dialect tokens
  // into the tags Telegram understands on paste: <b> <i> <s> <code> <a>.
  let source = line.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/^\s*#{1,6}\s+/, "").replace(TAG_PATTERN, "");
  source = escapeHtml(source);
  source = source.replace(/`([^`]+)`/g, "<code>$1</code>");
  source = source.replace(/\[([^\]]+)\]\(((?:[^()\s]|\([^()\s]*\))+)\)/g, (_m, label: string, url: string) => {
    const safe = isHttpUrl(url.replace(/&amp;/g, "&"));
    return safe ? `<a href="${escapeHtml(safe)}">${label}</a>` : label;
  });
  source = source.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  source = source.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, "<i>$1</i>");
  source = source.replace(/~~([^~]+)~~/g, "<s>$1</s>");
  return source;
}

export function htmlFromMarkdown(text: string): string {
  // <br> line breaks: bare newlines collapse when pasted as HTML.
  return text
    .split("\n")
    .map((line) => (/^\s*>\s?/.test(line) ? `<blockquote>${inlineHtml(line.replace(/^\s*>\s?/, ""))}</blockquote>` : inlineHtml(line)))
    .join("<br>");
}

/**
 * Copy a rendered selection exactly the way a manual copy does.
 *
 * The browser serialises the selected DOM itself, so it writes every flavour
 * it would for a hand-made selection: text/html and text/plain everywhere,
 * plus RTF and a web archive in Safari. The native macOS Telegram app reads
 * RTF and ignores a bare text/html payload, which is why a manual copy of the
 * preview kept formatting while an HTML-only clipboard write did not.
 * Returns false when the browser did not perform the copy.
 */
export function copyRenderedSelection(html: string): boolean {
  if (typeof document === "undefined" || typeof document.execCommand !== "function") return false;
  const host = document.createElement("div");
  // Off-screen but rendered and selectable: display:none or opacity:0 content
  // is skipped by some engines when serialising a selection.
  host.style.position = "fixed";
  host.style.left = "-100000px";
  host.style.top = "0";
  host.style.whiteSpace = "pre-wrap";
  host.setAttribute("aria-hidden", "true");
  host.innerHTML = html;
  document.body.appendChild(host);
  const selection = window.getSelection();
  const previous: Range[] = [];
  if (selection) for (let i = 0; i < selection.rangeCount; i += 1) previous.push(selection.getRangeAt(i));
  let done = false;
  try {
    const range = document.createRange();
    range.selectNodeContents(host);
    selection?.removeAllRanges();
    selection?.addRange(range);
    done = document.execCommand("copy") === true;
  } catch {
    done = false;
  } finally {
    selection?.removeAllRanges();
    previous.forEach((range) => selection?.addRange(range));
    host.remove();
  }
  return done;
}

/**
 * Write plain and HTML flavours through a copy-event handler, synchronously
 * inside the user's click. Fallback for engines where copying a rendered
 * selection fails; it cannot produce RTF.
 * Returns false when the browser did not run the copy event.
 */
export function copyRichText(plain: string, html: string): boolean {
  if (typeof document === "undefined" || typeof document.execCommand !== "function") return false;
  let done = false;
  const onCopy = (event: ClipboardEvent) => {
    if (!event.clipboardData) return;
    event.preventDefault();
    event.clipboardData.setData("text/plain", plain);
    event.clipboardData.setData("text/html", html);
    done = true;
  };
  document.addEventListener("copy", onCopy, true);
  const node = document.createElement("span");
  node.textContent = "​";
  node.style.position = "fixed";
  node.style.opacity = "0";
  document.body.appendChild(node);
  const selection = window.getSelection();
  const previous: Range[] = [];
  if (selection) for (let i = 0; i < selection.rangeCount; i += 1) previous.push(selection.getRangeAt(i));
  try {
    const range = document.createRange();
    range.selectNodeContents(node);
    selection?.removeAllRanges();
    selection?.addRange(range);
    document.execCommand("copy");
  } catch {
    done = false;
  } finally {
    selection?.removeAllRanges();
    previous.forEach((range) => selection?.addRange(range));
    node.remove();
    document.removeEventListener("copy", onCopy, true);
  }
  return done;
}
