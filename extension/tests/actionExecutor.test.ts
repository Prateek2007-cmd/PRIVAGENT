import { describe, expect, it } from "vitest";
import { isBlockedUrl, isConsoleUrl, isPageSettled } from "../src/actionExecutor";

describe("action executor URL guards", () => {
  it("blocks privileged and scriptable schemes", () => {
    for (const url of [
      "chrome://extensions",
      "chrome-extension://abc/popup.html",
      "chrome-search://local-ntp",
      "devtools://devtools/bundled/inspector.html",
      "view-source:https://example.com",
      "edge://settings",
      "about:blank",
      "file:///C:/Windows/win.ini",
      "data:text/html,<script>alert(1)</script>",
      "blob:https://example.com/uuid",
      "javascript:alert(1)",
      "vbscript:msgbox(1)",
      "filesystem:https://example.com/temporary/x",
      "mailto:a@b.com",
      "",
    ]) {
      expect(isBlockedUrl(url), url).toBe(true);
    }
  });

  it("allows ordinary http(s) pages", () => {
    expect(isBlockedUrl("https://example.com/a?b=1")).toBe(false);
    expect(isBlockedUrl("http://shop.local/checkout")).toBe(false);
  });

  it("recognizes the local console so the agent never drives itself", () => {
    expect(isConsoleUrl("http://localhost:5173/agent.html")).toBe(true);
    expect(isConsoleUrl("http://127.0.0.1:4173/")).toBe(true);
    expect(isConsoleUrl("http://localhost:8000/docs")).toBe(false);
    expect(isConsoleUrl("https://localhost:5173/")).toBe(true);
    expect(isConsoleUrl("https://example.com/")).toBe(false);
    expect(isConsoleUrl("not a url")).toBe(false);
  });
});

describe("page settlement", () => {
  it("accepts an interactive page immediately", () => {
    expect(isPageSettled({ ready: true, interactiveCount: 3, isConnecting: false })).toBe(true);
  });

  it("keeps waiting while the document is loading or WhatsApp is connecting", () => {
    expect(isPageSettled({ ready: false, interactiveCount: 9, isConnecting: false })).toBe(false);
    expect(isPageSettled({ ready: true, interactiveCount: 9, isConnecting: true })).toBe(false);
  });

  it("exits a low-element page after two stable samples", () => {
    const one = { ready: true, interactiveCount: 1, isConnecting: false };
    expect(isPageSettled(one, 1, 1)).toBe(false);
    expect(isPageSettled(one, 1, 2)).toBe(true);
  });

  it("does not make an empty page wait for the old 3.5s SPA timer", () => {
    const empty = { ready: true, interactiveCount: 0, isConnecting: false };
    expect(isPageSettled(empty, 0, 4)).toBe(false);
    expect(isPageSettled(empty, 0, 5)).toBe(true);
  });
});
