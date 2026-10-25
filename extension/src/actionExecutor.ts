import type {
  ActionResult,
  AgentError,
  BrowserAction,
  BrowserObservation,
} from "./contracts";
import { validateAction } from "./contracts";
import { findTarget } from "./targeting";
import { sanitizeObservation } from "./observer";

/** Injected content-script API shape (see lib/content.ts). */
interface AgentElement {
  targetId: string;
  role: string;
  name: string;
  tag: string;
  inputType?: string;
  cssSelector: string;
  enabled: boolean;
  visible: boolean;
  bounds?: { x: number; y: number; width: number; height: number };
}

interface AgentApi {
  list: () => Promise<AgentElement[]>;
  click: (targetId: string, name?: string) => Promise<{ ok: boolean; error?: string }>;
  clickCoords: (x: number, y: number) => Promise<{ ok: boolean; targetId?: string; error?: string }>;
  getCoords: (targetId: string, name?: string) => Promise<{ x: number; y: number; width: number; height: number } | null>;
  type: (targetId: string, text: string, clear: boolean, name?: string) => Promise<{ ok: boolean; error?: string }>;
  submit: (targetId: string, name?: string) => Promise<{ ok: boolean; error?: string }>;
  scroll: (d: "up" | "down" | "top" | "bottom", containerId?: string) => Promise<{ ok: boolean; scrollY: number }>;
}

async function withAgent(tabId: number): Promise<AgentApi> {
  await chrome.scripting.executeScript({
    target: { tabId },
    files: ["content.js"],
  });
  return {
    list: async () => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: () => (window as unknown as { __navigatorAgent?: { list: () => AgentElement[] } }).__navigatorAgent?.list() ?? [],
      });
      return (r?.result as AgentElement[]) ?? [];
    },
    getCoords: async (targetId: string, name?: string) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (id: string, nm?: string) =>
          (window as unknown as { __navigatorAgent?: { getCoords: (i: string, n?: string) => { x: number; y: number; width: number; height: number } | null } }).__navigatorAgent?.getCoords(id, nm) ?? null,
        args: [targetId, name ?? ""],
      });
      return (r?.result as { x: number; y: number; width: number; height: number } | null) ?? null;
    },
    click: async (targetId: string, name?: string) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (id: string, nm?: string) => (window as unknown as { __navigatorAgent?: { click: (i: string, n?: string) => { ok: boolean; error?: string } } }).__navigatorAgent?.click(id, nm) ?? { ok: false, error: "no agent" },
        args: [targetId, name ?? ""],
      });
      return (r?.result as { ok: boolean; error?: string }) ?? { ok: false, error: "execution failed" };
    },
    clickCoords: async (x: number, y: number) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (cx: number, cy: number) => (window as unknown as { __navigatorAgent?: { clickCoords: (px: number, py: number) => { ok: boolean; targetId?: string; error?: string } } }).__navigatorAgent?.clickCoords(cx, cy) ?? { ok: false, error: "no agent" },
        args: [x, y],
      });
      return (r?.result as { ok: boolean; targetId?: string; error?: string }) ?? { ok: false, error: "execution failed" };
    },
    type: async (targetId: string, text: string, clear: boolean, name?: string) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (id: string, val: string, cl: boolean, nm?: string) =>
          (window as unknown as { __navigatorAgent?: { type: (i: string, v: string, c: boolean, n?: string) => { ok: boolean; error?: string } } }).__navigatorAgent?.type(id, val, cl, nm) ?? { ok: false, error: "no agent" },
        args: [targetId, text, clear, name ?? ""],
      });
      return (r?.result as { ok: boolean; error?: string }) ?? { ok: false, error: "execution failed" };
    },
    submit: async (targetId: string, name?: string) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (id: string, nm?: string) =>
          (window as unknown as { __navigatorAgent?: { submit: (i: string, n?: string) => { ok: boolean; error?: string } } }).__navigatorAgent?.submit(id, nm) ?? { ok: false, error: "no agent" },
        args: [targetId, name ?? ""],
      });
      return (r?.result as { ok: boolean; error?: string }) ?? { ok: false, error: "execution failed" };
    },
    scroll: async (direction: "up" | "down" | "top" | "bottom", containerId?: string) => {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (dir: string, cid?: string) => (window as unknown as { __navigatorAgent?: { scroll: (d: any, c?: string) => { ok: boolean; scrollY: number } } }).__navigatorAgent?.scroll(dir as any, cid) ?? { ok: false, scrollY: 0 },
        args: [direction, containerId ?? ""],
      });
      return (r?.result as { ok: boolean; scrollY: number }) ?? { ok: false, scrollY: 0 };
    },
  };
}

const CONSOLE_PORTS = new Set(["5173", "4173"]);

/** True for the NAVIGATOR console itself, which the agent must never drive. */
export function isConsoleUrl(url: string): boolean {
  try {
    const u = new URL(url);
    return (u.hostname === "localhost" || u.hostname === "127.0.0.1" || u.hostname === "[::1]")
      && CONSOLE_PORTS.has(u.port || (u.protocol === "https:" ? "443" : "80"));
  } catch {
    return false;
  }
}

async function activeTab(): Promise<number | null> {
  // Prefer the tab the user is actually looking at.
  const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (active?.id !== undefined && !isBlockedUrl(active.url ?? "") && !isConsoleUrl(active.url ?? "")) {
    return active.id;
  }
  // Otherwise fall back to the most recently used permitted tab.
  const all = await chrome.tabs.query({});
  const candidates = all
    .filter(t => t.id !== undefined && !isBlockedUrl(t.url ?? "") && !isConsoleUrl(t.url ?? ""))
    .sort((a, b) => (b.lastAccessed ?? 0) - (a.lastAccessed ?? 0));
  return candidates[0]?.id ?? null;
}

async function currentUrl(tabId: number): Promise<string> {
  const tab = await chrome.tabs.get(tabId);
  return tab?.url ?? "";
}

const sleep = (ms: number): Promise<void> => new Promise(r => setTimeout(r, ms));

export interface PageSettlement {
  ready: boolean;
  interactiveCount: number;
  isConnecting: boolean;
}

/**
 * Decide whether the page has finished its interactive paint.
 *
 * - Three or more controls: usable immediately.
 * - One/two controls: wait for two stable samples (~80ms).
 * - Empty page: wait for five stable samples (~200ms) instead of the old
 *   3.5s SPA penalty. Chat apps that report "Connecting…" keep waiting.
 */
export function isPageSettled(
  status: PageSettlement,
  previousInteractive = -1,
  stableSamples = 0,
): boolean {
  if (!status.ready || status.isConnecting) return false;
  if (status.interactiveCount >= 3) return true;
  const required = status.interactiveCount === 0 ? 5 : 2;
  return status.interactiveCount === previousInteractive && stableSamples >= required;
}

function samePageUrl(a: string, b: string): boolean {
  if (a === b) return true;
  try {
    const ua = new URL(a);
    const ub = new URL(b);
    const norm = (p: string) => p.replace(/\/$/, "");
    return ua.origin === ub.origin
      && norm(ua.pathname) === norm(ub.pathname)
      && ua.search === ub.search
      && ua.hash === ub.hash;
  } catch {
    return false;
  }
}

async function pageStatus(tabId: number): Promise<PageSettlement | null> {
  try {
    const [res] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const text = document.body?.innerText || "";
        return {
          ready: document.readyState !== "loading",
          interactiveCount: document.querySelectorAll(
            'button, input, textarea, a[href], [role="button"], [contenteditable], [data-testid], ytd-thumbnail'
          ).length,
          isConnecting: text.includes("Connecting to WhatsApp") || text.includes("Loading your chats"),
        };
      },
    });
    return (res?.result as PageSettlement) ?? null;
  } catch {
    return null;
  }
}

/**
 * Resolve when DOM mutations have been quiet for `quietMs`, capped by `maxMs`.
 * A navigation that destroys the execution context rejects the injected
 * promise; callers then fall back to waitForStable().
 */
async function settleDom(tabId: number, quietMs = 120, maxMs = 700): Promise<void> {
  const quiet = Math.max(30, quietMs);
  const cap = Math.max(quiet, maxMs);
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: async (q: number, capMs: number) => {
        await new Promise<void>(resolve => {
          const root = document.documentElement;
          let done = false;
          let quietTimer: ReturnType<typeof setTimeout> | undefined;
          let maxTimer: ReturnType<typeof setTimeout> | undefined;
          let observer: MutationObserver | undefined;
          const finish = () => {
            if (done) return;
            done = true;
            if (quietTimer !== undefined) clearTimeout(quietTimer);
            if (maxTimer !== undefined) clearTimeout(maxTimer);
            observer?.disconnect();
            resolve();
          };
          if (!root) {
            finish();
            return;
          }
          maxTimer = setTimeout(finish, capMs);
          try {
            observer = new MutationObserver(() => {
              if (quietTimer !== undefined) clearTimeout(quietTimer);
              quietTimer = setTimeout(finish, q);
            });
            observer.observe(root, {
              childList: true,
              subtree: true,
              characterData: true,
            });
          } catch {
            // Observation unavailable: the quiet timer below still resolves.
          }
          quietTimer = setTimeout(finish, q);
        });
      },
      args: [quiet, cap],
    });
  } catch {
    // Navigation replaced the document mid-wait.
  }
}

/**
 * Post-action settle used by click/type-submit/press. Watches for a URL change,
 * a loading tab, or a newly activated tab (target=_blank), otherwise waits for
 * DOM quiescence. Returns the tab whose observation the next step should use.
 * It never activates an element, so single-click/single-submit is unchanged.
 */
async function settleAfterAction(tabId: number, beforeUrl: string, timeoutMs: number): Promise<number> {
  const cap = Math.max(150, Math.min(timeoutMs, 1200));
  await settleDom(tabId, 120, cap);

  // A click may have opened a new tab and made it active. Observe that tab,
  // not the old one, so the returned observation matches the next action's
  // active tab (and stays safe to cache).
  const active = await activeTab();
  if (active !== null && active !== tabId) {
    await waitForStable(active, timeoutMs);
    return active;
  }

  // A navigation can start just after the click's DOM churn settles.
  for (let i = 0; i < 4; i++) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (tab?.status === "loading" || (tab?.url && tab.url !== beforeUrl)) {
      await waitForStable(tabId, timeoutMs);
      return tabId;
    }
    await sleep(30);
  }
  return tabId;
}

const BLOCKED_SCHEMES = [
  "chrome://",
  "chrome-extension://",
  "chrome-search:",
  "devtools:",
  "view-source:",
  "edge://",
  "brave://",
  "opera://",
  "about:",
  "file://",
  "data:",
  "blob:",
  "javascript:",
  "vbscript:",
  "filesystem:",
];

export function isBlockedUrl(url: string): boolean {
  const u = (url || "").trim().toLowerCase();
  if (!u) return true;
  // Anything without an http(s) origin is off-limits for task navigation.
  if (!/^https?:\/\//.test(u)) return true;
  return BLOCKED_SCHEMES.some(s => u.startsWith(s));
}

export async function executeAction(action: BrowserAction, taskId: string): Promise<ActionResult> {
  const v = validateAction(action);
  if (!v.ok) {
    return { ok: false, error: { code: "PLAN_INVALID", message: `invalid action: ${v.error}`, recoverable: false } };
  }
  try {
    switch (action.type) {
      case "navigate": return await doNavigate(action);
      case "click": return await doClick(action, taskId);
      case "type": return await doType(action, taskId);
      case "press": return await doPress(action);
      case "scroll": return await doScroll(action);
      case "zoom": return await doZoom(action);
      case "new_tab": return await doNewTab(action);
      case "select_tab": {
        await chrome.tabs.update(action.params.tabId, { active: true });
        return { ok: true, evidence: `tab ${action.params.tabId} active` };
      }
      case "close_tab": {
        await chrome.tabs.remove(action.params.tabId);
        return { ok: true, evidence: `tab ${action.params.tabId} closed` };
      }
      case "wait": {
        const tabId = await activeTab();
        if (tabId !== null) await ensureTabActive(tabId);
        await new Promise(r => setTimeout(r, Math.min(action.timeoutMs, 10_000)));
        return {
          ok: true,
          observation: tabId !== null ? await observe(tabId) : undefined,
          evidence: "waited for page/app to settle"
        };
      }
      case "observe": {
        const tabId = await activeTab();
        if (tabId === null) return err("NAVIGATION_FAILED", "no active tab", false);
        return { ok: true, observation: await observe(tabId) };
      }
    }
  } catch (e) {
    return err("UNKNOWN", String(e), true);
  }
}

function err(code: AgentError["code"], message: string, recoverable: boolean): ActionResult {
  return { ok: false, error: { code, message, recoverable } };
}

async function doNavigate(action: Extract<BrowserAction, { type: "navigate" }>): Promise<ActionResult> {
  const tabId = await activeTab();
  if (tabId === null) return err("NAVIGATION_FAILED", "no active tab", false);
  const p = action.params;
  if (p.direction === "back") { await chrome.tabs.goBack(tabId); }
  else if (p.direction === "forward") { await chrome.tabs.goForward(tabId); }
  else if (p.direction === "reload") { await chrome.tabs.reload(tabId); }
  else if (p.url) {
    if (isBlockedUrl(p.url)) {
      return err("NAVIGATION_FAILED", `blocked scheme: ${p.url}`, false);
    }
    const cur = await chrome.tabs.get(tabId).catch(() => null);
    let targetTabId = tabId;
    if (cur?.url && isConsoleUrl(cur.url)) {
      const newTab = await chrome.tabs.create({ url: p.url, active: true });
      targetTabId = newTab.id!;
    } else {
      await chrome.tabs.update(tabId, { url: p.url });
    }
    const url = await waitForStable(targetTabId, action.timeoutMs, p.url);
    return { ok: true, observation: await observe(targetTabId), evidence: url };
  } else {
    return err("NAVIGATION_FAILED", "navigate needs url or direction", false);
  }
  const url = await waitForStable(tabId, action.timeoutMs);
  return { ok: true, observation: await observe(tabId), evidence: url };
}

async function ensureTabActive(tabId: number): Promise<void> {
  if (typeof chrome === "undefined" || !chrome.tabs) return;
  try {
    await chrome.tabs.update(tabId, { active: true });
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (tab?.windowId && chrome.windows) {
      await chrome.windows.update(tab.windowId, { focused: true }).catch(() => null);
    }
  } catch {}
}

async function cdpClick(tabId: number, x: number, y: number): Promise<boolean> {
  if (typeof chrome === "undefined" || !chrome.debugger) return false;
  await ensureTabActive(tabId);
  const target = { tabId };
  try {
    await chrome.debugger.attach(target, "1.3");
  } catch (e: any) {
    if (!e?.message?.includes("already attached")) {
      return false;
    }
  }

  let pressed = false;
  try {
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mouseMoved",
      x: Math.round(x),
      y: Math.round(y),
    });
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mousePressed",
      x: Math.round(x),
      y: Math.round(y),
      button: "left",
      buttons: 1,
      clickCount: 1,
      pointerType: "mouse",
    });
    // From here the page may already have received the activation. Report
    // success even if the release fails so callers never fall back to a
    // second in-page click.
    pressed = true;
    await new Promise(r => setTimeout(r, 20));
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mouseReleased",
      x: Math.round(x),
      y: Math.round(y),
      button: "left",
      buttons: 0,
      clickCount: 1,
      pointerType: "mouse",
    });
    await new Promise(r => setTimeout(r, 20));
    return true;
  } catch {
    return pressed;
  } finally {
    try {
      await chrome.debugger.detach(target);
    } catch {}
  }
}

async function cdpPressKey(tabId: number, key: string): Promise<boolean> {
  if (typeof chrome === "undefined" || !chrome.debugger) return false;
  await ensureTabActive(tabId);
  const target = { tabId };
  try {
    await chrome.debugger.attach(target, "1.3");
  } catch (e: any) {
    if (!e?.message?.includes("already attached")) {
      return false;
    }
  }

  let down = false;
  try {
    if (key === "Enter") {
      await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", {
        type: "keyDown",
        windowsVirtualKeyCode: 13,
        nativeVirtualKeyCode: 13,
        code: "Enter",
        key: "Enter",
        text: "\r",
        unmodifiedText: "\r",
      });
      // keyDown may already submit the form; treat a failed keyUp as a
      // successful attempt so no second Enter is dispatched.
      down = true;
      await new Promise(r => setTimeout(r, 15));
      await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", {
        type: "keyUp",
        windowsVirtualKeyCode: 13,
        nativeVirtualKeyCode: 13,
        code: "Enter",
        key: "Enter",
      });
      return true;
    }
    return false;
  } catch {
    return down;
  } finally {
    try {
      await chrome.debugger.detach(target);
    } catch {}
  }
}

async function doClick(action: Extract<BrowserAction, { type: "click" }>, taskId: string): Promise<ActionResult> {
  const tabId = await activeTab();
  if (tabId === null) return err("NAVIGATION_FAILED", "no active tab", false);
  await ensureTabActive(tabId);
  const before = await currentUrl(tabId);
  const agent = await withAgent(tabId);
  const cands = await agent.list();
  const target = findTarget(action.target, cands, "click");

  if (!target && action.target.coords) {
    // Exactly one click: trusted CDP input, with a single in-page fallback.
    const p = action.target.coords;
    const clicked = await cdpClick(tabId, p.x, p.y);
    const res = clicked
      ? { ok: true, targetId: undefined as string | undefined, error: undefined as string | undefined }
      : await agent.clickCoords(p.x, p.y);
    if (!res.ok) {
      return err("STALE_TARGET", res.error ?? "coordinate click failed", true);
    }
    const obsTab = await settleAfterAction(tabId, before, action.timeoutMs);
    const after = await currentUrl(obsTab);
    void taskId;
    return {
      ok: true,
      targetId: res.targetId,
      observation: await observe(obsTab),
      evidence: after !== before ? `navigated ${before} → ${after}` : `clicked coords (${p.x}, ${p.y})`,
    };
  }

  const targetId = target?.targetId || action.target.targetId || "";
  const targetName = target?.name || action.target.name || "";

  // 1. Get exact onscreen coordinates after scrollIntoView
  const coords = await agent.getCoords(targetId, targetName);
  let cdpSuccess = false;
  if (coords && coords.x > 0 && coords.y > 0) {
    // 2. One trusted click via Chrome DevTools Protocol
    cdpSuccess = await cdpClick(tabId, coords.x, coords.y);
  }

  // 3. In-page click only if CDP was unavailable — never both.
  if (!cdpSuccess) {
    const res = await agent.click(targetId, targetName);
    if (!res.ok) {
      return err("STALE_TARGET", res.error ?? `no matching element for ${JSON.stringify(action.target)}`, true);
    }
  }
  const obsTab = await settleAfterAction(tabId, before, action.timeoutMs);
  const after = await currentUrl(obsTab);
  void taskId;
  return {
    ok: true,
    targetId: targetId,
    observation: await observe(obsTab),
    evidence: after !== before ? `navigated ${before} → ${after}` : `clicked ${targetName || targetId}`,
  };
}

function findSubmitControl(cands: AgentElement[]): AgentElement | undefined {
  return cands.find(c => {
    if (!c.enabled || !c.visible) return false;
    const n = c.name.toLowerCase();
    if (n.includes("voice") || n.includes("ptt") || n.includes("microphone") || n.includes("record")) return false;
    return n === "send"
      || n.startsWith("send ")
      || n.includes("send message")
      || (c.role === "button" && n.includes("submit"));
  });
}

async function doType(action: Extract<BrowserAction, { type: "type" }>, taskId: string): Promise<ActionResult> {
  const tabId = await activeTab();
  if (tabId === null) return err("NAVIGATION_FAILED", "no active tab", false);
  await ensureTabActive(tabId);
  const before = await currentUrl(tabId);
  const agent = await withAgent(tabId);
  const cands = await agent.list();
  const target = findTarget(action.target, cands, "type");
  const targetId = target?.targetId || action.target.targetId || "";
  const targetName = target?.name || action.target.name || "";

  // 1. Ensure target element is focused via a single trusted click.
  const coords = await agent.getCoords(targetId, targetName);
  if (coords && coords.x > 0 && coords.y > 0) {
    await cdpClick(tabId, coords.x, coords.y);
  }

  // 2. Type only. Submission is a separate, single action below.
  const res = await agent.type(targetId, action.params.text, action.params.clear ?? true, targetName);
  if (!res.ok) return err("STALE_TARGET", res.error ?? "type failed", true);

  // Let a controlled input paint validation/autocomplete before this
  // observation is cached for the next planning step.
  await settleDom(tabId, 100, 300);

  let submitted = false;
  let obsTab = tabId;
  if (action.params.submit) {
    // Poll for the Send button instead of sleeping a fixed 150ms: exit as soon
    // as React/DOM exposes it, capped so a missing control still falls back.
    const renderDeadline = Date.now() + 400;
    let sendBtn = findSubmitControl(await agent.list());
    while (!sendBtn && Date.now() < renderDeadline) {
      await sleep(40);
      sendBtn = findSubmitControl(await agent.list());
    }
    if (sendBtn) {
      const sendCoords = await agent.getCoords(sendBtn.targetId, sendBtn.name);
      if (sendCoords && sendCoords.x > 0 && sendCoords.y > 0) {
        submitted = await cdpClick(tabId, sendCoords.x, sendCoords.y);
      }
    }
    // No button (or CDP unavailable): press Enter exactly once.
    if (!submitted) {
      submitted = await cdpPressKey(tabId, "Enter");
    }
    // Last resort: in-page Enter/form submit, still a single trigger.
    if (!submitted) {
      const fallback = await agent.submit(targetId, targetName);
      submitted = fallback.ok;
    }
    obsTab = await settleAfterAction(tabId, before, action.timeoutMs);
  }
  const after = await currentUrl(obsTab);
  void taskId;
  return {
    ok: true,
    targetId: targetId,
    observation: await observe(obsTab),
    evidence: action.params.submit
      ? (after !== before ? `submitted (${after})` : "typed + submitted")
      : "typed",
  };
}

async function doPress(action: Extract<BrowserAction, { type: "press" }>): Promise<ActionResult> {
  const tabId = await activeTab();
  if (tabId === null) return err("NAVIGATION_FAILED", "no active tab", false);
  await ensureTabActive(tabId);
  const before = await currentUrl(tabId);
  const key = action.params.key;

  if (key === "Enter") {
    const trusted = await cdpPressKey(tabId, "Enter");
    if (!trusted) {
      const agent = await withAgent(tabId);
      const fallback = await agent.submit("", "");
      if (!fallback.ok) return err("UNKNOWN", fallback.error ?? "Enter dispatch failed", true);
    }
  } else {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (k: string) => {
        const target = document.activeElement ?? document.body;
        const opts = { key: k, bubbles: true, cancelable: true };
        target.dispatchEvent(new KeyboardEvent("keydown", opts));
        target.dispatchEvent(new KeyboardEvent("keypress", opts));
        target.dispatchEvent(new KeyboardEvent("keyup", opts));
      },
      args: [key],
    });
  }
  const obsTab = await settleAfterAction(tabId, before, action.timeoutMs);
  return { ok: true, observation: await observe(obsTab), evidence: `pressed ${key}` };
}

async function doScroll(action: Extract<BrowserAction, { type: "scroll" }>): Promise<ActionResult> {
  const tabId = await activeTab();
  if (tabId === null) return err("NAVIGATION_FAILED", "no active tab", false);
  await ensureTabActive(tabId);
  const agent = await withAgent(tabId);
  const res = await agent.scroll(action.params.direction, action.params.containerId);
  // Infinite/lazy lists render after the scroll event; give them one paint so
  // the cached observation actually contains the newly revealed items.
  if (res.ok) await settleDom(tabId, 80, 400);
  return {
    ok: res.ok,
    observation: await observe(tabId),
    evidence: res.ok ? `scrollY=${res.scrollY}` : "scroll target not found",
    error: res.ok ? undefined : { code: "STALE_TARGET", message: "scroll container not found", recoverable: true },
  };
}

async function doZoom(action: Extract<BrowserAction, { type: "zoom" }>): Promise<ActionResult> {
  const tabId = await activeTab();
  if (tabId === null) return err("NAVIGATION_FAILED", "no active tab", false);
  const p = action.params;
  const current = await chrome.tabs.getZoom(tabId);
  let newZoom = current;
  if (p.direction === "in") newZoom = Math.min(current + 0.25, 3.0);
  else if (p.direction === "out") newZoom = Math.max(current - 0.25, 0.25);
  else if (p.direction === "reset") newZoom = 1.0;
  else if (p.factor !== undefined) newZoom = p.factor;
  await chrome.tabs.setZoom(tabId, newZoom);
  const after = await chrome.tabs.getZoom(tabId);
  return { ok: true, observation: await observe(tabId), evidence: `zoom=${after.toFixed(2)}` };
}

async function doNewTab(action: Extract<BrowserAction, { type: "new_tab" }>): Promise<ActionResult> {
  if (isBlockedUrl(action.params.url)) return err("NAVIGATION_FAILED", `blocked scheme: ${action.params.url}`, false);
  const tab = await chrome.tabs.create({ url: action.params.url, active: true });
  await waitForStable(tab.id!, action.timeoutMs, action.params.url);
  return { ok: true, observation: await observe(tab.id!), evidence: `opened ${action.params.url}` };
}

/** Wait until the navigation completes and the DOM is interactive and quiet. */
async function waitForStable(tabId: number, timeoutMs: number, expectedUrl?: string): Promise<string> {
  const start = Date.now();
  const budget = Math.max(200, Math.min(timeoutMs, 8000));
  const initial = await chrome.tabs.get(tabId).catch(() => null);
  const initialUrl = initial?.url ?? "";

  // 1. Wait for the navigation to actually begin. chrome.tabs.get can still
  // report the previous document as "complete" right after tabs.update().
  for (let i = 0; i < 12 && Date.now() - start < 600; i++) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) break;
    if (tab.status === "loading") break;
    if (expectedUrl ? samePageUrl(tab.url ?? "", expectedUrl) : tab.url !== initialUrl) break;
    await sleep(50);
  }

  // 2. Wait for the network load to complete.
  while (Date.now() - start < budget) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (tab?.status === "complete" && tab.url) break;
    await sleep(50);
  }

  // 3. Hydration: fast poll, exit as soon as the DOM is interactive and
  // stable. Low-element pages no longer pay a fixed 3.5s penalty.
  const hydrateDeadline = Math.min(Date.now() + 3500, start + budget);
  let previousInteractive = -1;
  let stableSamples = 0;
  while (Date.now() < hydrateDeadline) {
    const status = await pageStatus(tabId);
    if (status) {
      if (status.interactiveCount === previousInteractive) {
        stableSamples++;
      } else {
        previousInteractive = status.interactiveCount;
        stableSamples = 0;
      }
      if (isPageSettled(status, previousInteractive, stableSamples)) break;
    }
    await sleep(40);
  }

  const tab = await chrome.tabs.get(tabId).catch(() => null);
  return tab?.url ?? "";
}

async function captureScreenshot(tabId: number): Promise<string | undefined> {
  if (typeof chrome === "undefined" || !chrome.tabs?.captureVisibleTab) return undefined;
  try {
    const tab = await chrome.tabs.get(tabId);
    if (!tab.windowId) return undefined;
    const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 45 });
    return dataUrl;
  } catch {
    return undefined;
  }
}

export async function observe(tabId: number): Promise<BrowserObservation> {
  await ensureTabActive(tabId);
  const tab = await chrome.tabs.get(tabId);
  const agent = await withAgent(tabId);
  let list = await agent.list();

  // If the list is empty, poll briefly for SPA elements before returning an
  // empty observation. 100ms polls replace the old 400ms cadence, so a page
  // that hydrates in 60ms is observed immediately instead of after 400ms.
  if (list.length === 0) {
    for (let i = 0; i < 10; i++) {
      await sleep(100);
      list = await agent.list();
      if (list.length > 0) break;
    }
  }

  const [meta] = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => ({ y: scrollY, w: innerWidth, h: innerHeight, dpr: devicePixelRatio }),
  });
  const m = meta?.result ?? { y: 0, w: 0, h: 0, dpr: 1 };
  const screenshot = await captureScreenshot(tabId);
  return sanitizeObservation({
    tabId,
    url: tab.url ?? "",
    title: tab.title ?? "",
    interactive: list.map(e => ({
      targetId: e.targetId,
      role: e.role,
      name: e.name,
      tag: e.tag || "el",
      inputType: e.inputType,
      visible: e.visible,
      enabled: e.enabled,
      // Values are never read at observation time; the privacy layer is only
      // a second line of defense.
      value: undefined,
      bounds: e.bounds,
    })),
    scroll: { y: m.y, viewport: { w: m.w, h: m.h } },
    dpr: m.dpr,
    screenshot,
  });
}
