import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cdpKeySequence,
  getOrCreateActiveTab,
  isBlockedUrl,
  isConsoleUrl,
  isDrivableTabUrl,
  isPageSettled,
} from "../src/actionExecutor";

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

describe("native CDP key sequences", () => {
  it("pairs a rawKeyDown with a keyUp for Enter", () => {
    const events = cdpKeySequence("enter");

    expect(events).toHaveLength(2);
    expect(events[0]).toMatchObject({
      type: "rawKeyDown", key: "Enter", code: "Enter",
      windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13, modifiers: 0,
    });
    expect(events[1]).toMatchObject({
      type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13,
    });
  });

  it("uses the control modifier for select-all on Windows/Linux", () => {
    const events = cdpKeySequence("selectAll", false);

    expect(events[0]).toMatchObject({
      type: "rawKeyDown", key: "a", code: "KeyA",
      windowsVirtualKeyCode: 65, modifiers: 2,
    });
    expect(events[1]).toMatchObject({ type: "keyUp", modifiers: 2 });
  });

  it("uses the meta modifier for select-all on macOS", () => {
    expect(cdpKeySequence("selectAll", true)[0]).toMatchObject({ modifiers: 4 });
  });

  it("clears with Backspace through the native pipeline", () => {
    const events = cdpKeySequence("backspace");

    expect(events[0]).toMatchObject({
      type: "rawKeyDown", key: "Backspace", code: "Backspace",
      windowsVirtualKeyCode: 8, modifiers: 0,
    });
    expect(events[1]).toMatchObject({ type: "keyUp", key: "Backspace" });
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

type TabLike = { id: number; url: string; lastAccessed?: number };

function installChrome(tabs: TabLike[]) {
  const create = vi.fn(async (props: chrome.tabs.CreateProperties) => ({ id: 99, url: props.url }));
  const query = vi.fn(async (q: chrome.tabs.QueryInfo) => (q.active ? tabs.slice(0, 1) : tabs));
  vi.stubGlobal("chrome", { tabs: { query, create } });
  return { create, query };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("drivable tab URLs", () => {
  it("accepts a fresh about:blank tab as a starting point", () => {
    expect(isDrivableTabUrl("about:blank")).toBe(true);
    expect(isDrivableTabUrl("")).toBe(true);
    expect(isDrivableTabUrl("https://web.whatsapp.com/")).toBe(true);
  });

  it("still refuses privileged pages, the console, and scriptable schemes", () => {
    for (const url of [
      "chrome://newtab",
      "chrome-extension://abc/popup.html",
      "devtools://devtools/bundled/inspector.html",
      "http://localhost:5173/agent.html",
      "data:text/html,<script>alert(1)</script>",
      "javascript:alert(1)",
    ]) {
      expect(isDrivableTabUrl(url), url).toBe(false);
    }
  });

  it("keeps about:blank invalid as a navigation target", () => {
    expect(isBlockedUrl("about:blank")).toBe(true);
  });
});

describe("tab lifecycle", () => {
  it("creates a tab when the browser only has privileged pages", async () => {
    const { create } = installChrome([
      { id: 1, url: "chrome://newtab" },
      { id: 2, url: "http://localhost:5173/agent.html" },
      { id: 3, url: "chrome-extension://abc/popup.html" },
    ]);

    await expect(getOrCreateActiveTab()).resolves.toBe(99);
    expect(create).toHaveBeenCalledWith({ url: "about:blank", active: true });
  });

  it("creates a tab when no tab exists at all", async () => {
    const { create } = installChrome([]);
    await expect(getOrCreateActiveTab()).resolves.toBe(99);
    expect(create).toHaveBeenCalledWith({ url: "about:blank", active: true });
  });

  it("reuses an existing about:blank tab instead of creating one per action", async () => {
    const { create } = installChrome([{ id: 7, url: "about:blank" }]);
    await expect(getOrCreateActiveTab()).resolves.toBe(7);
    expect(create).not.toHaveBeenCalled();
  });

  it("prefers the active ordinary tab", async () => {
    const { create } = installChrome([
      { id: 5, url: "https://example.com/" },
      { id: 6, url: "https://other.example/" },
    ]);
    await expect(getOrCreateActiveTab()).resolves.toBe(5);
    expect(create).not.toHaveBeenCalled();
  });
});
