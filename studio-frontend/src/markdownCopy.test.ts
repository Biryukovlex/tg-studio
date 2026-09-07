import { describe, it, expect } from "vitest";
import { copyRichText, htmlFromMarkdown, plainFromMarkdown } from "./markdownCopy";

describe("markdownCopy", () => {
  it("renders the dialect to Telegram-friendly HTML with <br> line breaks", () => {
    const html = htmlFromMarkdown("**Title**\nBody with a [source](https://example.com/a?b=1&c=2) and *emphasis*\n> quoted");
    expect(html).toBe(
      '<b>Title</b><br>Body with a <a href="https://example.com/a?b=1&amp;c=2">source</a> and <i>emphasis</i><br><blockquote>quoted</blockquote>',
    );
  });

  it("escapes prose and neutralises out-of-dialect markup and unsafe links", () => {
    expect(htmlFromMarkdown("a < b and c > d")).toBe("a &lt; b and c &gt; d");
    expect(htmlFromMarkdown("# Heading <script>x</script> ![i](https://x) [j](javascript:alert(1))")).toBe("Heading x i j");
    expect(htmlFromMarkdown("`code` and ~~gone~~")).toBe("<code>code</code> and <s>gone</s>");
    // URLs with balanced parentheses (Wikipedia-style) stay whole.
    expect(htmlFromMarkdown("[w](https://en.wikipedia.org/wiki/Foo_(bar)) end")).toBe(
      '<a href="https://en.wikipedia.org/wiki/Foo_(bar)">w</a> end',
    );
    expect(plainFromMarkdown("[w](https://en.wikipedia.org/wiki/Foo_(bar))")).toBe("w (https://en.wikipedia.org/wiki/Foo_(bar))");
  });

  it("produces plain text that keeps URLs readable", () => {
    expect(plainFromMarkdown("**T**\n[site](https://example.com)\n[https://example.com](https://example.com)\n> q")).toBe(
      "T\nsite (https://example.com)\nhttps://example.com\nq",
    );
  });

  it("reports false instead of throwing when the synchronous copy path is unavailable", () => {
    const original = document.execCommand;
    // jsdom has no execCommand; make the absence explicit.
    (document as unknown as { execCommand?: unknown }).execCommand = undefined;
    try {
      expect(copyRichText("plain", "<b>plain</b>")).toBe(false);
    } finally {
      (document as unknown as { execCommand?: unknown }).execCommand = original;
    }
  });
});
